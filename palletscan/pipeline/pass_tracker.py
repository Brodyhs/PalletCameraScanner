"""PassTracker: segments -> business events, with the account-for-everything
miss path.

One pallet produces many decodes across frames; the tracker collapses them
by payload within a dedup window into a single PassEvent. A motion segment
in which no payload reaches the confirmation threshold becomes a MissEvent
— but only after its post-roll deadline passes, so the evidence burst can
include frames from after the segment closed.

Miss evidence is assembled from three pieces: a pre-roll snapshot taken
when the segment opens (a long segment outlives the rolling buffer's
horizon), a bounded in-segment frame reservoir, and the post-roll pulled
from the rolling buffer at the deadline.

Single-threaded by design: called only from the pipeline thread, in frame
order. All clocks are the frame source clock (``Frame.ts``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from palletscan.config import BufferConfig, DedupConfig
from palletscan.events.evidence import EvidenceRef, EvidenceWriter
from palletscan.pipeline.decode_engine import PassDecodeContext
from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.types import (
    DecodeResult,
    Event,
    Frame,
    MissEvent,
    PassEvent,
    Roi,
    SegmentEvent,
    now_iso,
)

if TYPE_CHECKING:
    from palletscan.pipeline.segment_recorder import SegmentRecorder

log = logging.getLogger(__name__)


class _FrameReservoir:
    """Bounded, order-preserving sample of a growing frame sequence.

    Keeps every ``stride``-th frame; when the kept list exceeds ``cap`` it
    drops every other kept frame and doubles the stride, so an arbitrarily
    long segment yields evenly spaced evidence in bounded memory.
    """

    __slots__ = ("_cap", "_stride", "_seen", "frames")

    def __init__(self, cap: int = 256) -> None:
        self._cap = cap
        self._stride = 1
        self._seen = 0
        self.frames: list[Frame] = []

    def add(self, frame: Frame) -> None:
        if self._seen % self._stride == 0:
            self.frames.append(frame)
            if len(self.frames) > self._cap:
                self.frames = self.frames[::2]
                self._stride *= 2
        self._seen += 1


@dataclass(slots=True)
class _SegmentState:
    candidate_id: str
    open_frame: int
    open_ts: float
    ctx: PassDecodeContext = field(default_factory=PassDecodeContext)
    decodes: list[DecodeResult] = field(default_factory=list)
    payload_counts: dict[str, int] = field(default_factory=dict)
    evidence: _FrameReservoir = field(default_factory=_FrameReservoir)
    #: Last full-res decode ROI observed for this segment (recording mode
    #: only; set via note_roi). None when the segment was never decode-eligible
    #: — replay then falls back to full-frame.
    last_roi: Roi | None = None


@dataclass(slots=True)
class _PendingMiss:
    candidate_id: str
    source_id: str
    open_frame: int
    open_ts: float
    close_frame: int
    close_ts: float
    deadline_ts: float
    frames: list[Frame]


@dataclass(slots=True)
class _PendingRecord:
    """A motion segment awaiting its post-roll before being recorded.

    Mirrors :class:`_PendingMiss` but exists for BOTH outcomes: ``outcome``
    is ``"pass"`` (``payloads`` = the decoded ground-truth label) or
    ``"miss"`` (``payloads`` = ``[]``). One is appended per segment on both
    finalize branches, so N concurrent segments yield N independent records.
    """

    candidate_id: str
    source_id: str
    open_frame: int
    open_ts: float
    close_frame: int
    close_ts: float
    deadline_ts: float
    frames: list[Frame]
    outcome: str
    payloads: list[str]
    symbologies: list[str]
    roi: Roi | None


class PassTracker:
    """Aggregates decode results per segment and emits Pass/Miss events."""

    def __init__(
        self,
        dedup_cfg: DedupConfig,
        buffer_cfg: BufferConfig,
        evidence: EvidenceWriter,
        buffer: RollingFrameBuffer,
        emit: Callable[[Event], None],
        source_id: str,
        confirmations: int = 1,
        ts_to_wall: Callable[[float], str] | None = None,
        recorder: "SegmentRecorder | None" = None,
        record_post_s: float = 2.0,
        record_tracking: str = "single",
        record_engine: str = "legacy",
    ) -> None:
        self._dedup = dedup_cfg
        self._buffer_cfg = buffer_cfg
        self._evidence = evidence
        self._buffer = buffer
        self._emit = emit
        self._source_id = source_id
        self._confirmations = max(1, int(confirmations))
        # Trial recording tap (Phase 6.1); None => wholly inert, default path
        # byte-identical. Everything recording-related is behind
        # ``self._recorder is not None``.
        self._recorder = recorder
        self._record_post_s = record_post_s
        self._record_tracking = record_tracking
        self._record_engine = record_engine
        self._pending_records: list[_PendingRecord] = []
        # Maps a source-clock ts to the wall-clock ISO stamp of that
        # moment (sources with a known epoch provide it). Events are then
        # attributed to when the pallet PASSED, not when the deferred
        # finalize ran — an outage-deferred miss must not land in the
        # reconnect's report window (REVIEW finding b12).
        self._ts_to_wall = ts_to_wall
        #: candidate_id -> open segment. Single mode keeps a dict-of-one
        #: (behaviorally identical); multi mode runs concurrent segments, each
        #: with its OWN reservoir + decode context + miss/finalize block, so a
        #: decoded pallet never swallows a co-located undecoded one's miss
        #: (the account-for-everything win).
        self._open: dict[str, _SegmentState] = {}
        self._recent: dict[str, float] = {}  # payload -> last_seen_ts
        self._pending: list[_PendingMiss] = []
        self.passes_emitted = 0
        self.misses_emitted = 0
        self.passes_merged = 0
        self.evidence_failures = 0

    # -- pipeline-thread API ------------------------------------------------

    def on_segment_open(self, ev: SegmentEvent) -> PassDecodeContext:
        """Start tracking a candidate; returns its decode context.

        Concurrency is legal (multi mode): a DUPLICATE candidate_id is the
        only error worth warning about — re-opening an already-tracked id
        would silently drop the in-flight segment's decodes/evidence.
        """
        if ev.candidate_id in self._open:
            log.warning(
                "duplicate open for segment %s; finalizing the previous one",
                ev.candidate_id,
            )
            self._finalize_segment(
                self._open[ev.candidate_id], ev.frame_index, ev.ts
            )
        seg = _SegmentState(
            candidate_id=ev.candidate_id, open_frame=ev.frame_index, open_ts=ev.ts
        )
        self._open[ev.candidate_id] = seg
        # Snapshot the pre-roll now: by the time a long segment closes and
        # its post-roll deadline passes, these frames are long evicted from
        # the rolling buffer.
        for f in self._buffer.extract(
            ev.ts - self._buffer_cfg.pre_s, float("inf")
        ):
            seg.evidence.add(f)
        return seg.ctx

    @property
    def open_ctx(self) -> PassDecodeContext | None:
        """Decode context of the single open segment, if exactly one is open.

        Single-mode convenience (dict-of-one). Multi-mode callers route per
        candidate_id and must not rely on this.
        """
        if len(self._open) == 1:
            return next(iter(self._open.values())).ctx
        return None

    @property
    def has_open(self) -> bool:
        """True if any segment is currently open (multi-mode idle-scan gate)."""
        return bool(self._open)

    def ctx_for(self, candidate_id: str) -> PassDecodeContext | None:
        """Decode context for a specific open segment, or None."""
        seg = self._open.get(candidate_id)
        return seg.ctx if seg is not None else None

    def note_roi(self, candidate_id: str | None, roi: Roi) -> None:
        """Record the last decode ROI for a segment (recording mode only).

        No-op unless a recorder is attached, so the default path pays only a
        single ``is None`` check at each decode site. The recorded ROI lets
        replay crop exactly as the live decoder did; an un-noted segment
        replays full-frame.
        """
        if self._recorder is None:
            return
        seg = self._open.get(candidate_id) if candidate_id is not None else None
        if seg is not None:
            seg.last_roi = roi

    def on_decode(
        self, candidate_id: str, results: list[DecodeResult]
    ) -> None:
        """Attach decode results from the current frame to one open segment."""
        seg = self._open.get(candidate_id)
        if seg is None or not results:
            return
        seg.decodes.extend(results)
        for d in results:
            n = seg.payload_counts.get(d.payload, 0) + 1
            seg.payload_counts[d.payload] = n
            if n >= self._confirmations:
                seg.ctx.confirmed = True

    def on_segment_close(self, ev: SegmentEvent) -> None:
        seg = self._open.pop(ev.candidate_id, None)
        if seg is None:
            log.warning("close for unknown segment %s", ev.candidate_id)
            return
        self._finalize_segment(seg, ev.frame_index, ev.ts)

    def on_frame(self, frame: Frame) -> None:
        """Per-frame hook: collect in-segment evidence, then advance the clock.

        EVERY open segment's reservoir is fed, so concurrent segments each
        accumulate their own evidence burst.
        """
        for seg in self._open.values():
            seg.evidence.add(frame)
        self.on_frame_ts(frame.ts)

    def on_frame_ts(self, ts: float) -> None:
        """Advance the clock: finalize pending misses whose post-roll is full."""
        while self._pending and self._pending[0].deadline_ts <= ts:
            self._finalize_miss(self._pending.pop(0))
        while self._pending_records and self._pending_records[0].deadline_ts <= ts:
            self._finalize_record(self._pending_records.pop(0))

    def flush_pending(self) -> None:
        """Finalize every pending miss now, post-roll deadlines or not.

        Used at end-of-stream and at a source discontinuity (watchdog
        reconnect): frames after the break are not pallet-exit evidence,
        so waiting out the deadline cannot add anything — it can only let
        the break's ts jump pull wrong frames into the burst.
        """
        while self._pending:
            self._finalize_miss(self._pending.pop(0))
        while self._pending_records:
            self._finalize_record(self._pending_records.pop(0))

    def seed_recent(self, entries: dict[str, float]) -> None:
        """Seed the payload dedup window from a previous process's stored
        passes, with timestamps already mapped into THIS process's source
        clock (they are typically negative — before process start). A
        pallet pass spanning a supervisor restart merges instead of
        double-counting as a second business pass (REVIEW finding 10).
        """
        for payload, ts in entries.items():
            if ts > self._recent.get(payload, float("-inf")):
                self._recent[payload] = ts

    def flush(self) -> None:
        """End-of-stream: finalize everything with whatever frames exist."""
        for seg in list(self._open.values()):
            log.warning("flush with open segment %s", seg.candidate_id)
            last = seg.decodes[-1].frame_index if seg.decodes else seg.open_frame
            last_ts = seg.decodes[-1].ts if seg.decodes else seg.open_ts
            self._finalize_segment(seg, last, last_ts)
        self._open.clear()
        self.flush_pending()

    # -- internals ------------------------------------------------------------

    def _finalize_segment(
        self, seg: _SegmentState, close_frame: int, close_ts: float
    ) -> None:
        by_payload: dict[str, list[DecodeResult]] = {}
        for d in seg.decodes:
            by_payload.setdefault(d.payload, []).append(d)
        confirmed = {
            p: ds
            for p, ds in by_payload.items()
            if len(ds) >= self._confirmations
        }
        if not confirmed:
            self._pending.append(
                _PendingMiss(
                    candidate_id=seg.candidate_id,
                    source_id=self._source_id,
                    open_frame=seg.open_frame,
                    open_ts=seg.open_ts,
                    close_frame=close_frame,
                    close_ts=close_ts,
                    deadline_ts=close_ts + self._buffer_cfg.post_s,
                    frames=seg.evidence.frames,
                )
            )
            if self._recorder is not None:
                self._queue_record(seg, close_frame, close_ts, "miss", {})
            return
        for payload, decodes in confirmed.items():
            last_seen = self._recent.get(payload)
            if last_seen is not None and close_ts - last_seen <= self._dedup.window_s:
                self.passes_merged += 1
                log.info(
                    "pass %s merged into recent sighting (dt=%.1fs)",
                    payload,
                    close_ts - last_seen,
                )
                continue
            # Refresh the window only on emit: merged sightings must not
            # keep extending suppression indefinitely.
            self._recent[payload] = close_ts
            first = decodes[0]
            self._emit(
                PassEvent(
                    payload=payload,
                    symbology=first.symbology,
                    first_seen_ts=seg.open_ts,
                    last_seen_ts=close_ts,
                    decode_count=len(decodes),
                    cameras={self._source_id: len(decodes)},
                    best_frame=(first.source_id, first.frame_index),
                    candidate_ids=[seg.candidate_id],
                    event_id=str(uuid.uuid4()),
                    wall_time_iso=self._wall_at(close_ts),
                    first_decode_ts=first.ts,
                    camera_detail={
                        self._source_id: {
                            "first_seen_ts": seg.open_ts,
                            "first_decode_ts": first.ts,
                            "last_seen_ts": close_ts,
                            "decode_count": len(decodes),
                        }
                    },
                )
            )
            self.passes_emitted += 1
        # A decoded segment is a "pass" record regardless of whether its
        # PassEvent emitted or merged into a recent sighting — the segment
        # DID decode. Queued after the emit loop so pass timing is untouched.
        if self._recorder is not None:
            self._queue_record(seg, close_frame, close_ts, "pass", confirmed)
        # Expire stale dedup entries lazily.
        cutoff = close_ts - self._dedup.window_s
        self._recent = {p: t for p, t in self._recent.items() if t >= cutoff}

    def _wall_at(self, ts: float) -> str:
        """Wall-clock stamp for a source-clock instant (now when unmapped)."""
        return self._ts_to_wall(ts) if self._ts_to_wall is not None else now_iso()

    def _harvest_post(
        self, close_ts: float, frames: list[Frame], post_s: float
    ) -> list[Frame]:
        """Segment frames + the ``post_s``-second post-roll pulled from the
        rolling buffer.

        Pre-roll + segment frames were captured while the segment was open;
        only the post-roll still lives in the rolling buffer. Quiet-gap
        frames (ts > close_ts) were already sampled into the reservoir while
        the segment wound down, so the post-roll must exclude them or the
        burst double-writes frames and overstates the frame count (REVIEW
        finding b11). Builds a NEW list — never mutates the shared reservoir.

        Shared by ``_finalize_miss`` and ``_finalize_record`` so the two
        harvests' extract/dedup logic cannot drift. The window length is a
        parameter, not a constant: the miss burst uses ``buffer.post_s`` while
        a recording uses its own ``recording.post_s`` (``record_post_s``), so
        raising the recording knob genuinely captures more trailing evidence
        rather than silently staying pinned to the miss window. The rolling
        buffer horizon is sized (``app.py``) to retain at least this far past
        a close whenever recording is enabled.
        """
        have = {f.frame_index for f in frames}
        post = [
            f
            for f in self._buffer.extract(close_ts, close_ts + post_s)
            if f.ts > close_ts and f.frame_index not in have
        ]
        return frames + post

    def _queue_record(
        self,
        seg: _SegmentState,
        close_frame: int,
        close_ts: float,
        outcome: str,
        confirmed: dict[str, list[DecodeResult]],
    ) -> None:
        """Append this segment's _PendingRecord (recording mode only). The
        payloads are the ground-truth label: the decoded payloads for a pass,
        ``[]`` for a miss."""
        payloads = list(confirmed.keys())
        symbologies = [confirmed[p][0].symbology.value for p in payloads]
        self._pending_records.append(
            _PendingRecord(
                candidate_id=seg.candidate_id,
                source_id=self._source_id,
                open_frame=seg.open_frame,
                open_ts=seg.open_ts,
                close_frame=close_frame,
                close_ts=close_ts,
                deadline_ts=close_ts + self._record_post_s,
                frames=seg.evidence.frames,
                outcome=outcome,
                payloads=payloads,
                symbologies=symbologies,
                roi=seg.last_roi,
            )
        )

    def _finalize_record(self, record: _PendingRecord) -> None:
        """Write one recorded segment burst off the pipeline thread.

        Non-fatal by construction: submit() never blocks and never raises,
        and the recorder writer degrades on storage failure — recording must
        never perturb pass/miss accounting.
        """
        assert self._recorder is not None  # only queued when recording
        frames = self._harvest_post(
            record.close_ts, record.frames, self._record_post_s
        )
        meta: dict[str, object] = {
            "schema": "recording/v1",
            "outcome": record.outcome,
            "payloads": record.payloads,
            "symbologies": record.symbologies,
            "source_id": record.source_id,
            "tracking": self._record_tracking,
            "engine": self._record_engine,
            "segment_frames": [record.open_frame, record.close_frame],
            "segment_ts": [record.open_ts, record.close_ts],
        }
        if record.roi is not None:
            r = record.roi
            meta["roi"] = [r.x, r.y, r.w, r.h]
        self._recorder.submit(record.candidate_id, frames, meta)

    def _finalize_miss(self, miss: _PendingMiss) -> None:
        frames = self._harvest_post(
            miss.close_ts, miss.frames, self._buffer_cfg.post_s
        )
        try:
            ref = self._evidence.write_burst(
                miss.candidate_id,
                frames,
                meta={
                    "source_id": miss.source_id,
                    "segment_frames": [miss.open_frame, miss.close_frame],
                    "segment_ts": [miss.open_ts, miss.close_ts],
                    "reason": "motion segment ended with no decode",
                },
            )
        except Exception as exc:
            # write_burst degrades on OSError itself; this layer catches
            # anything else. The pending entry was destructively popped, so
            # raising here would eat the MissEvent forever (REVIEW finding
            # 1) — emit evidence-less and flagged instead.
            log.exception(
                "evidence burst for %s failed; emitting evidence-less miss",
                miss.candidate_id,
            )
            ref = EvidenceRef(directory=None, frame_count=0, error=repr(exc))
        if ref.error is not None:
            self.evidence_failures += 1
        self._emit(
            MissEvent(
                candidate_id=miss.candidate_id,
                source_id=miss.source_id,
                start_ts=miss.open_ts,
                end_ts=miss.close_ts,
                first_frame=miss.open_frame,
                last_frame=miss.close_frame,
                evidence_dir="" if ref.directory is None else str(ref.directory),
                evidence_frame_count=ref.frame_count,
                event_id=str(uuid.uuid4()),
                wall_time_iso=self._wall_at(miss.close_ts),
                evidence_error=ref.error,
            )
        )
        self.misses_emitted += 1
        log.warning(
            "MISS %s [%s] frames %d-%d evidence=%s%s",
            miss.candidate_id,
            miss.source_id,
            miss.open_frame,
            miss.close_frame,
            ref.directory,
            "" if ref.error is None else f" (EVIDENCE FAILED: {ref.error})",
        )
