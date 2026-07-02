"""MotionGate: cheap motion detection that gates the expensive decode work.

Runs on heavily downscaled grayscale (INTER_AREA averaging also crushes
per-pixel sensor noise), maintains a pass-candidate segment state machine
(debounced open, quiet-frame close), and emits a full-resolution ROI around
the moving region.

TIER 2 (``motion.tracking: multi``): the same downscaled mask is segmented
into per-object blobs, associated across frames (greedy IoU + centroid
fallback with split/merge hysteresis), and each track runs the SAME debounce
+ open/close lifecycle INDEPENDENTLY — so two pallets crossing the zone at
once are each accounted for. ``tracking: single`` (default) is the historical
whole-mask-union path, byte-for-byte unchanged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2
import numpy as np

from palletscan.config import MotionAlgorithm, MotionConfig
from palletscan.types import (
    Frame,
    MotionResult,
    MotionTrack,
    Roi,
    SegmentEvent,
    SegmentKind,
)

log = logging.getLogger(__name__)


@dataclass
class _Track:
    """Per-object debounce/lifecycle state in multi mode.

    Holds the SAME scalar segment fields the single path keeps, but one set
    per tracked object. ``track_id`` is stable for the object's life; the
    segment candidate id (``open_id``) is minted only once the debounce opens.
    """

    track_id: str
    roi: Roi
    centroid: tuple[float, float]
    area_px: int
    age: int = 1
    missed: int = 0
    active_streak: int = 0
    quiet_streak: int = 0
    open_id: str | None = None
    open_backdate: tuple[int, float] | None = None  # first active frame
    last_active: tuple[int, float] | None = None
    # Merge hysteresis: consecutive frames this track's focus blob (assigned,
    # or best gate-eligible when unmatched) has been contended by another
    # track. Reset whenever the ambiguity clears.
    merge_streak: int = 0


@dataclass
class _Blob:
    """One connected component this frame: full-res ROI + downscaled centroid."""

    roi: Roi
    centroid: tuple[float, float]
    area_px: int


class MotionGate:
    """Per-source motion gate. Call :meth:`update` once per frame in order.

    ``run_token`` (default: UTC HHMMSS at construction) makes candidate ids
    unique across process restarts: the per-process segment counter starts
    at 0 every run, and two same-day runs would otherwise mint the same
    ``<source>-000001`` id — whose evidence directories then silently merge
    and byte-overwrite each other (REVIEW finding 5).
    """

    def __init__(
        self,
        cfg: MotionConfig,
        source_id: str,
        run_token: str | None = None,
        *,
        nominal_fps: float | None = None,
    ) -> None:
        self._cfg = cfg
        self._source_id = source_id
        self._run_token = (
            run_token
            if run_token is not None
            else time.strftime("%H%M%S", time.gmtime())
        )
        # Time-based debounce (motion.open_s/quiet_s): converted to per-camera
        # frame counts once here so the hot loop stays integer-streak based.
        # Falls back to the frame-count knobs when the source's rate is
        # unknown — a silent default-fps guess would skew exactly the A/B
        # wall-clock parity these knobs exist to protect.
        self._open_frames = cfg.open_frames
        self._quiet_frames = cfg.quiet_frames
        if cfg.open_s is not None or cfg.quiet_s is not None:
            if nominal_fps is None or nominal_fps <= 0:
                log.warning(
                    "motion.open_s/quiet_s set but source %s has no nominal "
                    "fps; falling back to frame-count debounce "
                    "(open_frames=%d, quiet_frames=%d)",
                    source_id,
                    cfg.open_frames,
                    cfg.quiet_frames,
                )
            else:
                if cfg.open_s is not None:
                    self._open_frames = max(1, round(cfg.open_s * nominal_fps))
                if cfg.quiet_s is not None:
                    self._quiet_frames = max(
                        1, round(cfg.quiet_s * nominal_fps)
                    )
        self._prev_small: np.ndarray | None = None
        self._mog2 = self._make_mog2()
        self._mog2_primed = False
        self._kernel = np.ones((3, 3), np.uint8)
        # Segment state (single mode)
        self._segment_count = 0
        self._active_streak = 0
        self._quiet_streak = 0
        self._open_id: str | None = None
        self._open_backdate: tuple[int, float] | None = None  # first active frame
        self._last_active: tuple[int, float] | None = None
        # Per-object track state (multi mode)
        self._tracks: dict[str, _Track] = {}
        self._track_count = 0

    def _make_mog2(self) -> cv2.BackgroundSubtractorMOG2 | None:
        return (
            cv2.createBackgroundSubtractorMOG2(detectShadows=False)
            if self._cfg.algorithm is MotionAlgorithm.MOG2
            else None
        )

    def _mask(self, small: np.ndarray) -> np.ndarray | None:
        """Binary motion mask on the downscaled frame, or None on warm-up."""
        if self._mog2 is not None:
            raw = self._mog2.apply(small)
            if not self._mog2_primed:
                # MOG2 has no background model yet on the first frame and
                # reports the entire frame as foreground.
                self._mog2_primed = True
                return None
            return cv2.dilate((raw > 0).astype(np.uint8), self._kernel)
        prev, self._prev_small = self._prev_small, small
        if prev is None:
            return None
        diff = cv2.absdiff(small, prev)
        mask = (diff > self._cfg.diff_threshold).astype(np.uint8)
        return cv2.dilate(mask, self._kernel)

    def _downscale(self, image: np.ndarray) -> np.ndarray:
        """Full-frame INTER_AREA downscale to exactly (sw, sh).

        Must be a single averaging resize: a strided pre-slice DECIMATES
        instead of averaging, and at common resolutions the follow-up resize
        becomes a no-op, so raw per-pixel sensor noise floods the frame diff
        (phantom whole-frame motion; REVIEW_bringup_4d95b67 finding 1).
        """
        h, w = image.shape
        sw = self._cfg.downscale_width
        sh = max(1, round(h * sw / w))
        return cv2.resize(image, (sw, sh), interpolation=cv2.INTER_AREA)

    def update(self, frame: Frame) -> tuple[MotionResult, list[SegmentEvent]]:
        """Classify one frame; emit 0..N segment open/close events.

        Single mode emits at most one event (wrapped in a list); multi mode
        may open/close several tracks in a frame.
        """
        cfg = self._cfg
        h, w = frame.image.shape
        sw = cfg.downscale_width
        small = self._downscale(frame.image)
        mask = self._mask(small)

        if cfg.tracking == "multi" and mask is not None:
            return self._update_multi(frame, mask, small)

        roi: Roi | None = None
        motion_frac = 0.0
        if mask is not None:
            motion_frac = float(np.count_nonzero(mask)) / mask.size
            # The > 0 guard keeps min_area_frac: 0 from reducing an empty
            # mask (np.nonzero -> empty arrays -> min() raises).
            if motion_frac >= cfg.min_area_frac and motion_frac > 0.0:
                ys, xs = np.nonzero(mask)
                scale = w / sw
                roi = Roi(
                    x=int(xs.min() * scale),
                    y=int(ys.min() * scale),
                    w=int((xs.max() + 1 - xs.min()) * scale),
                    h=int((ys.max() + 1 - ys.min()) * scale),
                ).pad(cfg.roi_pad_px).clamp(frame.image.shape)

        active = roi is not None
        event: SegmentEvent | None = None
        if active:
            self._quiet_streak = 0
            self._active_streak += 1
            if self._active_streak == 1:
                self._open_backdate = (frame.frame_index, frame.ts)
            self._last_active = (frame.frame_index, frame.ts)
            if self._open_id is None and self._active_streak >= self._open_frames:
                self._segment_count += 1
                self._open_id = (
                    f"{self._source_id}-{self._run_token}"
                    f"-{self._segment_count:06d}"
                )
                backdate = self._open_backdate or (frame.frame_index, frame.ts)
                event = SegmentEvent(
                    kind=SegmentKind.OPEN,
                    candidate_id=self._open_id,
                    frame_index=backdate[0],
                    ts=backdate[1],
                )
        else:
            self._active_streak = 0
            if self._open_id is not None:
                self._quiet_streak += 1
                if self._quiet_streak >= self._quiet_frames:
                    event = self._close_segment()

        return (
            MotionResult(
                active=active,
                candidate_id=self._open_id,
                roi=roi,
                motion_frac=motion_frac,
            ),
            [] if event is None else [event],
        )

    # -- multi-object mode -----------------------------------------------------

    def _segment_blobs(
        self, frame: Frame, mask: np.ndarray, sw: int
    ) -> list[_Blob]:
        """Connected components of the mask -> per-object full-res ROIs.

        Drops the background label, components below the per-blob area floor,
        and keeps the ``track_max_objects`` largest. Same Roi math as the
        single path (scale -> pad -> clamp).
        """
        cfg = self._cfg
        h, w = frame.image.shape
        scale = w / sw
        # Coalesce one object's fragmented motion blob into a SINGLE component
        # before labeling: a moving QR/DM mask breaks into pieces, and without
        # this each piece mints a churny micro-track that opens + closes as a
        # spurious miss. The kernel bridges intra-object gaps but stays small
        # enough not to fuse genuinely-separate objects. Multi-only; the single
        # union-bbox path never labels, so its behavior is unchanged.
        k = round(cfg.track_close_kernel_frac * sw)
        if k >= 2:
            ksz = k | 1  # force odd
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksz, ksz))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        n, _labels, stats, centroids = cv2.connectedComponentsWithStats(
            mask, connectivity=8
        )
        floor = cfg.track_min_blob_area_frac * mask.size
        cand: list[_Blob] = []
        for label in range(1, n):  # 0 is background
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < floor:
                continue
            bx = int(stats[label, cv2.CC_STAT_LEFT])
            by = int(stats[label, cv2.CC_STAT_TOP])
            bw = int(stats[label, cv2.CC_STAT_WIDTH])
            bh = int(stats[label, cv2.CC_STAT_HEIGHT])
            roi = (
                Roi(
                    x=int(bx * scale),
                    y=int(by * scale),
                    w=int(bw * scale),
                    h=int(bh * scale),
                )
                .pad(cfg.roi_pad_px)
                .clamp(frame.image.shape)
            )
            cx, cy = centroids[label]
            cand.append(_Blob(roi=roi, centroid=(float(cx), float(cy)), area_px=area))
        cand.sort(key=lambda b: b.area_px, reverse=True)
        return cand[: cfg.track_max_objects]

    def _update_multi(
        self, frame: Frame, mask: np.ndarray, small: np.ndarray
    ) -> tuple[MotionResult, list[SegmentEvent]]:
        cfg = self._cfg
        sw = cfg.downscale_width
        motion_frac = float(np.count_nonzero(mask)) / mask.size
        # The whole-mask floor still gates everything: below it, nothing is
        # moving and every live track simply ages toward a quiet close.
        if motion_frac >= cfg.min_area_frac and motion_frac > 0.0:
            blobs = self._segment_blobs(frame, mask, sw)
        else:
            blobs = []

        events = self._associate(frame, blobs, small)

        # Primary track = largest-area OPEN track this frame (mirror single
        # mode's roi/candidate_id onto it for preview + back-compat). Only
        # OPEN tracks UPDATED this frame (missed == 0) surface as
        # MotionTracks: app.py decodes every surfaced ROI, and an unmatched
        # track's ROI is stale — single mode likewise gates decode on THIS
        # frame's motion. Unmatched open tracks stay alive inside the gate
        # for miss accounting and close on the ordinary quiet path
        # (REVIEW_bringup_4d95b67 finding 15). A track's ``track_id`` IS the
        # segment candidate_id (``open_id``), so app.py routes decodes to the
        # right PassTracker segment with no extra lookup.
        result_tracks = tuple(
            MotionTrack(
                track_id=t.open_id,
                roi=t.roi,
                centroid=t.centroid,
                area_px=t.area_px,
                age=t.age,
                missed=t.missed,
            )
            for t in self._tracks.values()
            if t.open_id is not None and t.missed == 0
        )
        primary = max(
            (
                t
                for t in self._tracks.values()
                if t.open_id is not None and t.missed == 0
            ),
            key=lambda t: t.area_px,
            default=None,
        )
        return (
            MotionResult(
                active=bool(result_tracks),
                candidate_id=primary.open_id if primary is not None else None,
                roi=primary.roi if primary is not None else None,
                motion_frac=motion_frac,
                tracks=result_tracks,
            ),
            events,
        )

    def _associate(
        self, frame: Frame, blobs: list[_Blob], small: np.ndarray
    ) -> list[SegmentEvent]:
        """Greedy one-to-one blob<->track association, then per-track debounce.

        Candidate pairs satisfy IoU >= track_iou_gate OR centroid distance <=
        track_centroid_max_frac * frame_diag; scored by IoU (centroid distance
        breaks ties). Unmatched blobs become provisional NEW tracks; unmatched
        tracks age (debounced close, never instant). Split/merge ambiguities
        must persist track_merge_hysteresis_frames before committing.
        """
        cfg = self._cfg
        sh, sw = small.shape
        diag = float((sw**2 + sh**2) ** 0.5)
        centroid_max = cfg.track_centroid_max_frac * diag

        track_ids = list(self._tracks.keys())
        # Build candidate (score, -dist, track_idx, blob_idx) pairs.
        pairs: list[tuple[float, float, int, int]] = []
        for ti, tid in enumerate(track_ids):
            t = self._tracks[tid]
            for bi, b in enumerate(blobs):
                iou = self._roi_iou(t.roi, b.roi)
                dist = self._centroid_dist(t.centroid, b.centroid)
                if iou >= cfg.track_iou_gate or dist <= centroid_max:
                    pairs.append((iou, -dist, ti, bi))
        pairs.sort(reverse=True)

        matched_tracks: dict[int, int] = {}  # track_idx -> blob_idx
        matched_blobs: dict[int, int] = {}  # blob_idx -> track_idx
        for _iou, _ndist, ti, bi in pairs:
            if ti in matched_tracks or bi in matched_blobs:
                continue
            matched_tracks[ti] = bi
            matched_blobs[bi] = ti

        # MERGE detection (hysteresis). Each track FOCUSES on exactly one blob
        # per frame: its greedy-assigned blob, or — when left unmatched — its
        # best gate-eligible blob. A blob focused by >= 2 live tracks is a
        # genuine merge candidate: two objects' masks fused into one component
        # that greedy could hand to only one of them. A track matched 1:1 to
        # its own distinct blob is NEVER a merge candidate — the old detection
        # on the FULL gate-eligible pair set force-closed side-by-side tracks
        # that were each riding a real object, and could increment a streak
        # twice per frame (REVIEW_bringup_4d95b67 finding 9).
        focus: dict[int, int] = dict(matched_tracks)
        for _iou, _ndist, ti, bi in pairs:  # sorted best-first: first wins
            focus.setdefault(ti, bi)
        contenders_by_blob: dict[int, list[int]] = {}
        for ti, bi in focus.items():
            contenders_by_blob.setdefault(bi, []).append(ti)

        events: list[SegmentEvent] = []

        # Persist the ambiguity track_merge_hysteresis_frames before
        # committing; on commit, CLOSE the smaller / undecoded absorbed track
        # so its miss finalizes (never silently fold one track's frames into
        # another's segment).
        absorbed: set[int] = set()
        contending: set[int] = set()
        for bi, contenders in contenders_by_blob.items():
            if len(contenders) < 2:
                continue
            contenders.sort(
                key=lambda ti: self._tracks[track_ids[ti]].area_px,
                reverse=True,
            )
            keeper = contenders[0]
            for ti in contenders[1:]:
                t = self._tracks[track_ids[ti]]
                contending.add(ti)
                # One focus blob per track -> exactly one increment per frame.
                t.merge_streak += 1
                if t.merge_streak < cfg.track_merge_hysteresis_frames:
                    continue
                # Commit the merge: the keeper takes the fused blob; the
                # absorbed track is closed so its (undecoded) frames still
                # finalize.
                if matched_blobs.get(bi) == ti:
                    # The fused blob was greedy-assigned to the absorbed
                    # track: hand it to the keeper so it still updates a
                    # track THIS frame.
                    matched_tracks.pop(ti, None)
                    prev = matched_tracks.get(keeper)
                    if prev is not None and prev != bi:
                        # The keeper abandons its old blob: clear its
                        # matched_blobs entry so the blob can still
                        # update/spawn a track this frame. (Unreachable
                        # under the focus rule — a matched keeper's focus
                        # IS bi — kept as a local invariant.)
                        matched_blobs.pop(prev, None)
                    matched_tracks[keeper] = bi
                    matched_blobs[bi] = keeper
                else:
                    matched_tracks.pop(ti, None)
                ev = self._close_track(track_ids[ti])
                if ev is not None:
                    events.append(ev)
                absorbed.add(ti)

        # Apply per-track lifecycle to matched + unmatched tracks.
        for ti, tid in enumerate(track_ids):
            if ti in absorbed:
                continue
            live = self._tracks.get(tid)
            if live is None:  # closed+removed during merge commit
                continue
            if ti not in contending:
                # Ambiguity cleared (or never present): reset the hysteresis.
                live.merge_streak = 0
            if ti in matched_tracks:
                ev = self._track_active(frame, live, blobs[matched_tracks[ti]])
                if ev is not None:
                    events.append(ev)
            else:
                ev = self._track_inactive(frame, tid, live)
                if ev is not None:
                    events.append(ev)

        # Unmatched blobs -> provisional NEW tracks (over-segmentation by
        # design: a loud extra miss beats a silently swallowed one). This also
        # implements the SPLIT case — when one track suddenly spans two blobs,
        # it keeps the larger and the orphan becomes a fresh track.
        for bi, b in enumerate(blobs):
            if bi in matched_blobs:
                continue
            ev = self._spawn_track(frame, b)
            if ev is not None:
                events.append(ev)

        return events

    def _track_active(
        self, frame: Frame, t: _Track, b: _Blob
    ) -> SegmentEvent | None:
        t.roi = b.roi
        t.centroid = b.centroid
        t.area_px = b.area_px
        t.age += 1
        t.missed = 0
        t.quiet_streak = 0
        t.active_streak += 1
        if t.active_streak == 1:
            t.open_backdate = (frame.frame_index, frame.ts)
        t.last_active = (frame.frame_index, frame.ts)
        return self._maybe_open(t, frame)

    def _maybe_open(self, t: _Track, frame: Frame) -> SegmentEvent | None:
        """Mint the segment id + OPEN event once the debounce is satisfied.

        The ONE open-condition evaluation, shared by update and spawn: a
        freshly spawned track (active_streak == 1) must open on its spawn
        frame when open_frames == 1, matching single mode's debounce
        (REVIEW_bringup_4d95b67 finding 9)."""
        if t.open_id is not None or t.active_streak < self._open_frames:
            return None
        self._segment_count += 1
        t.open_id = (
            f"{self._source_id}-{self._run_token}"
            f"-{self._segment_count:06d}"
        )
        backdate = t.open_backdate or (frame.frame_index, frame.ts)
        return SegmentEvent(
            kind=SegmentKind.OPEN,
            candidate_id=t.open_id,
            frame_index=backdate[0],
            ts=backdate[1],
        )

    def _track_inactive(
        self, frame: Frame, tid: str, t: _Track
    ) -> SegmentEvent | None:
        t.missed += 1
        # merge_streak is NOT reset here: an unmatched track contending for
        # the fused blob (a genuine merge leaves the loser unmatched) must
        # keep accruing its hysteresis; _associate resets the streak for
        # every track whose ambiguity cleared.
        t.active_streak = 0
        if t.open_id is not None:
            t.quiet_streak += 1
            if t.quiet_streak >= self._quiet_frames:
                return self._close_track(tid)
        else:
            # A never-opened provisional track that goes quiet for the close
            # window is dropped (it never debounced into a segment).
            t.quiet_streak += 1
            if t.quiet_streak >= self._quiet_frames:
                self._tracks.pop(tid, None)
        return None

    def _spawn_track(self, frame: Frame, b: _Blob) -> SegmentEvent | None:
        self._track_count += 1
        tid = f"track-{self._track_count:06d}"
        t = _Track(
            track_id=tid,
            roi=b.roi,
            centroid=b.centroid,
            area_px=b.area_px,
            active_streak=1,
            open_backdate=(frame.frame_index, frame.ts),
            last_active=(frame.frame_index, frame.ts),
        )
        self._tracks[tid] = t
        # active_streak == 1 can already satisfy open_frames == 1: evaluate
        # the open condition instead of silently deferring to the next match.
        return self._maybe_open(t, frame)

    def _close_track(self, tid: str) -> SegmentEvent | None:
        t = self._tracks.pop(tid, None)
        if t is None or t.open_id is None or t.last_active is None:
            return None
        return SegmentEvent(
            kind=SegmentKind.CLOSE,
            candidate_id=t.open_id,
            frame_index=t.last_active[0],
            ts=t.last_active[1],
        )

    @staticmethod
    def _roi_iou(a: Roi, b: Roi) -> float:
        ax2, ay2 = a.x + a.w, a.y + a.h
        bx2, by2 = b.x + b.w, b.y + b.h
        ix = max(0, min(ax2, bx2) - max(a.x, b.x))
        iy = max(0, min(ay2, by2) - max(a.y, b.y))
        inter = ix * iy
        if inter == 0:
            return 0.0
        union = a.w * a.h + b.w * b.h - inter
        return inter / union if union > 0 else 0.0

    @staticmethod
    def _centroid_dist(
        a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        return float(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5)

    # -- single-mode segment helpers ------------------------------------------

    def _close_segment(self) -> SegmentEvent:
        assert self._open_id is not None and self._last_active is not None
        event = SegmentEvent(
            kind=SegmentKind.CLOSE,
            candidate_id=self._open_id,
            frame_index=self._last_active[0],
            ts=self._last_active[1],
        )
        self._open_id = None
        self._quiet_streak = 0
        self._active_streak = 0
        return event

    def flush(self) -> list[SegmentEvent]:
        """Close any open segment(s) at end-of-stream."""
        if self._cfg.tracking == "multi":
            events: list[SegmentEvent] = []
            for tid in list(self._tracks.keys()):
                ev = self._close_track(tid)
                if ev is not None:
                    events.append(ev)
            self._tracks.clear()
            return events
        if self._open_id is not None:
            return [self._close_segment()]
        return []

    def break_segment(self) -> list[SegmentEvent]:
        """Source discontinuity (watchdog reconnect): hard segment boundary.

        Closes any open segment(s) at the last *observed* active frame — the
        pre-gap pallet — so motion present at reconnect can never glue onto
        it (a decoded pallet on the far side would swallow the undecoded
        one's MissEvent; REVIEW finding 2, critical). Also resets the
        debounce streaks AND the motion model: post-gap frames re-warm like
        stream start, because diffing against a pre-gap reference (or an
        MOG2 background learned before the gap, with exposure possibly
        re-negotiated) manufactures phantom whole-frame motion.
        """
        events = self.flush()
        self._active_streak = 0
        self._quiet_streak = 0
        self._open_backdate = None
        self._last_active = None
        self._tracks.clear()
        self._prev_small = None
        self._mog2 = self._make_mog2()
        self._mog2_primed = False
        return events
