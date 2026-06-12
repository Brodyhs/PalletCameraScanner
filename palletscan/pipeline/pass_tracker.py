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
from palletscan.events.evidence import EvidenceWriter
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
    ) -> None:
        self._dedup = dedup_cfg
        self._buffer_cfg = buffer_cfg
        self._evidence = evidence
        self._buffer = buffer
        self._emit = emit
        self._source_id = source_id
        self._confirmations = max(1, int(confirmations))
        self._open: _SegmentState | None = None
        self._recent: dict[str, float] = {}  # payload -> last_seen_ts
        self._pending: list[_PendingMiss] = []
        self.passes_emitted = 0
        self.misses_emitted = 0
        self.passes_merged = 0

    # -- pipeline-thread API ------------------------------------------------

    def on_segment_open(self, ev: SegmentEvent) -> PassDecodeContext:
        """Start tracking a candidate; returns its decode context."""
        if self._open is not None:
            log.warning(
                "segment %s opened while %s still open; closing previous",
                ev.candidate_id,
                self._open.candidate_id,
            )
            self._finalize_segment(self._open, ev.frame_index, ev.ts)
        self._open = _SegmentState(
            candidate_id=ev.candidate_id, open_frame=ev.frame_index, open_ts=ev.ts
        )
        # Snapshot the pre-roll now: by the time a long segment closes and
        # its post-roll deadline passes, these frames are long evicted from
        # the rolling buffer.
        for f in self._buffer.extract(
            ev.ts - self._buffer_cfg.pre_s, float("inf")
        ):
            self._open.evidence.add(f)
        return self._open.ctx

    @property
    def open_ctx(self) -> PassDecodeContext | None:
        """Decode context of the currently open segment, if any."""
        return self._open.ctx if self._open is not None else None

    def on_decode(self, results: list[DecodeResult]) -> None:
        """Attach decode results from the current frame to the open segment."""
        if self._open is None or not results:
            return
        seg = self._open
        seg.decodes.extend(results)
        for d in results:
            n = seg.payload_counts.get(d.payload, 0) + 1
            seg.payload_counts[d.payload] = n
            if n >= self._confirmations:
                seg.ctx.confirmed = True

    def on_segment_close(self, ev: SegmentEvent) -> None:
        if self._open is None or self._open.candidate_id != ev.candidate_id:
            log.warning("close for unknown segment %s", ev.candidate_id)
            return
        self._finalize_segment(self._open, ev.frame_index, ev.ts)
        self._open = None

    def on_frame(self, frame: Frame) -> None:
        """Per-frame hook: collect in-segment evidence, then advance the clock."""
        if self._open is not None:
            self._open.evidence.add(frame)
        self.on_frame_ts(frame.ts)

    def on_frame_ts(self, ts: float) -> None:
        """Advance the clock: finalize pending misses whose post-roll is full."""
        while self._pending and self._pending[0].deadline_ts <= ts:
            self._finalize_miss(self._pending.pop(0))

    def flush(self) -> None:
        """End-of-stream: finalize everything with whatever frames exist."""
        if self._open is not None:
            log.warning("flush with open segment %s", self._open.candidate_id)
            last = self._open.decodes[-1].frame_index if self._open.decodes else self._open.open_frame
            last_ts = self._open.decodes[-1].ts if self._open.decodes else self._open.open_ts
            self._finalize_segment(self._open, last, last_ts)
            self._open = None
        while self._pending:
            self._finalize_miss(self._pending.pop(0))

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
                    wall_time_iso=now_iso(),
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

    def _finalize_miss(self, miss: _PendingMiss) -> None:
        # Pre-roll + segment frames were captured while the segment was
        # open; only the post-roll still lives in the rolling buffer.
        post = [
            f
            for f in self._buffer.extract(
                miss.close_ts, miss.close_ts + self._buffer_cfg.post_s
            )
            if f.ts > miss.close_ts
        ]
        frames = miss.frames + post
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
        self._emit(
            MissEvent(
                candidate_id=miss.candidate_id,
                source_id=miss.source_id,
                start_ts=miss.open_ts,
                end_ts=miss.close_ts,
                first_frame=miss.open_frame,
                last_frame=miss.close_frame,
                evidence_dir=str(ref.directory),
                evidence_frame_count=ref.frame_count,
                event_id=str(uuid.uuid4()),
                wall_time_iso=now_iso(),
            )
        )
        self.misses_emitted += 1
        log.warning(
            "MISS %s [%s] frames %d-%d evidence=%s",
            miss.candidate_id,
            miss.source_id,
            miss.open_frame,
            miss.close_frame,
            ref.directory,
        )
