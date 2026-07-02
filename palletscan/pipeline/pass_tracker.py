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
    SegmentEvent,
    now_iso,
)

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
    ) -> None:
        self._dedup = dedup_cfg
        self._buffer_cfg = buffer_cfg
        self._evidence = evidence
        self._buffer = buffer
        self._emit = emit
        self._source_id = source_id
        self._confirmations = max(1, int(confirmations))
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

    def flush_pending(self) -> None:
        """Finalize every pending miss now, post-roll deadlines or not.

        Used at end-of-stream and at a source discontinuity (watchdog
        reconnect): frames after the break are not pallet-exit evidence,
        so waiting out the deadline cannot add anything — it can only let
        the break's ts jump pull wrong frames into the burst.
        """
        while self._pending:
            self._finalize_miss(self._pending.pop(0))

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
        # Expire stale dedup entries lazily.
        cutoff = close_ts - self._dedup.window_s
        self._recent = {p: t for p, t in self._recent.items() if t >= cutoff}

    def _wall_at(self, ts: float) -> str:
        """Wall-clock stamp for a source-clock instant (now when unmapped)."""
        return self._ts_to_wall(ts) if self._ts_to_wall is not None else now_iso()

    def _finalize_miss(self, miss: _PendingMiss) -> None:
        # Pre-roll + segment frames were captured while the segment was
        # open; only the post-roll still lives in the rolling buffer.
        # Quiet-gap frames (ts > close_ts) were already sampled into the
        # reservoir while the segment wound down, so the post-roll must
        # exclude them or the burst double-writes frames and overstates
        # evidence_frame_count (REVIEW finding b11).
        have = {f.frame_index for f in miss.frames}
        post = [
            f
            for f in self._buffer.extract(
                miss.close_ts, miss.close_ts + self._buffer_cfg.post_s
            )
            if f.ts > miss.close_ts and f.frame_index not in have
        ]
        frames = miss.frames + post
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
