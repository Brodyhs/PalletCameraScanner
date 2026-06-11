"""PipelineRunner: wires source -> motion -> decode -> tracker -> bus -> sinks.

Thread topology (deliberately small — see ASSUMPTIONS.md):

    [source thread] --frame_q (drop-oldest)--> [pipeline thread] --event_q--> [bus thread]

MotionGate, DecodeEngine and PassTracker run inline on the pipeline thread
(strict per-frame ordering, shared per-segment state); decode parallelism
lives inside the executor that DecodeEngine fans variant work onto.

There is no global mutable state: every stage, queue and counter hangs off
this object, constructed from an AppConfig.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field

from palletscan.config import AppConfig, ExecutorKind
from palletscan.events.bus import EventBus
from palletscan.events.evidence import EvidenceWriter
from palletscan.events.sinks import ConsoleSink, JsonlSink, Sink, SqliteSink
from palletscan.pipeline.decode_engine import DecodeEngine
from palletscan.pipeline.motion_gate import MotionGate
from palletscan.pipeline.pass_tracker import PassTracker
from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.reliability.queues import SENTINEL, DroppingQueue
from palletscan.sources.base import FrameSource
from palletscan.sources.synthetic import SyntheticSource
from palletscan.types import (
    Event,
    Frame,
    GroundTruthRecord,
    MissEvent,
    PassEvent,
    SegmentKind,
)

log = logging.getLogger(__name__)

_EVENT_COLLECT_CAP = 100_000


@dataclass(slots=True)
class Reconciliation:
    """Ground-truth accounting for synthetic runs."""

    truth_passes: int
    decoded: int
    missed: int
    unaccounted: list[str] = field(default_factory=list)

    @property
    def read_rate(self) -> float:
        return self.decoded / self.truth_passes if self.truth_passes else 1.0


@dataclass(slots=True)
class RunSummary:
    frames: int
    frames_dropped: int
    passes: int
    passes_merged: int
    misses: int
    events_handled: int
    sink_errors: int
    frame_errors: int = 0
    reconciliation: Reconciliation | None = None

    @property
    def unaccounted(self) -> int:
        return len(self.reconciliation.unaccounted) if self.reconciliation else 0

    def format(self) -> str:
        lines = [
            "── run summary ──",
            f"frames processed : {self.frames} (dropped {self.frames_dropped})",
            f"pass events      : {self.passes} (+{self.passes_merged} merged)",
            f"miss events      : {self.misses}",
            f"events handled   : {self.events_handled} (sink errors {self.sink_errors})",
        ]
        if self.frame_errors:
            lines.append(f"frame errors     : {self.frame_errors}")
        if self.reconciliation is not None:
            r = self.reconciliation
            lines += [
                f"truth passes     : {r.truth_passes}",
                f"  decoded        : {r.decoded} ({r.read_rate:.1%} read rate)",
                f"  missed (flagged): {r.missed}",
                f"  UNACCOUNTED    : {len(r.unaccounted)} {r.unaccounted or ''}",
            ]
        return "\n".join(lines)


def reconcile_truth(
    truth: list[GroundTruthRecord], events: list[Event], fps: float
) -> Reconciliation:
    """Match ground truth against emitted events.

    A truth pass is accounted for iff its payload was decoded (PassEvent)
    or a MissEvent overlaps its time range (the account-for-everything
    invariant). Returns the list of unaccounted payloads (must be empty).
    """
    decoded_payloads = {e.payload for e in events if isinstance(e, PassEvent)}
    misses = [e for e in events if isinstance(e, MissEvent)]
    decoded = 0
    missed = 0
    unaccounted: list[str] = []
    # Slack covers only per-frame boundary wiggle (backdated opens, a close
    # that trails by a frame). It must stay below the smallest idle gap
    # between passes, or a neighbor's miss vouches for a silently dropped
    # pass and defeats the very check this exists for.
    slack = 2.0 / fps
    for rec in truth:
        if rec.payload in decoded_payloads:
            decoded += 1
            continue
        t0, t1 = rec.first_frame / fps, rec.last_frame / fps
        if any(m.start_ts - slack <= t1 and m.end_ts + slack >= t0 for m in misses):
            missed += 1
        else:
            unaccounted.append(rec.payload)
    return Reconciliation(
        truth_passes=len(truth),
        decoded=decoded,
        missed=missed,
        unaccounted=unaccounted,
    )


class _ListSink(Sink):
    """In-memory sink used for run summaries and tests (capped)."""

    def __init__(self, cap: int = _EVENT_COLLECT_CAP) -> None:
        self.events: list[Event] = []
        self._cap = cap

    def handle(self, event: Event) -> None:
        if len(self.events) < self._cap:
            self.events.append(event)


class PipelineRunner:
    """One source's pipeline. Construct via :meth:`from_config`, call
    :meth:`run` once."""

    def __init__(self, cfg: AppConfig, source: FrameSource, sinks: list[Sink]) -> None:
        self._cfg = cfg
        self.source = source
        self._collector = _ListSink()
        self._bus = EventBus(sinks + [self._collector])
        self._frame_q = DroppingQueue(maxsize=64)
        self._executor: Executor = (
            ThreadPoolExecutor(
                max_workers=cfg.decode.workers, thread_name_prefix="decode"
            )
            if cfg.decode.executor is ExecutorKind.THREAD
            else ProcessPoolExecutor(max_workers=cfg.decode.workers)
        )
        self._gate = MotionGate(cfg.motion, source.source_id)
        self._engine = DecodeEngine(cfg.decode, self._executor)
        # The tracker snapshots pre-roll/segment evidence while a segment is
        # open, so the buffer only ever serves pre-roll (at open) and
        # post-roll (at the miss deadline) lookbacks.
        horizon = cfg.buffer.pre_s + cfg.buffer.post_s + 1.0
        fps = source.nominal_fps or 30.0
        self._buffer = RollingFrameBuffer(
            horizon_s=horizon, maxlen=max(512, int(horizon * fps * 1.25))
        )
        self._tracker = PassTracker(
            dedup_cfg=cfg.dedup,
            buffer_cfg=cfg.buffer,
            evidence=EvidenceWriter(cfg.evidence),
            buffer=self._buffer,
            emit=self._bus.publish,
            source_id=source.source_id,
            confirmations=cfg.decode.confirmations,
        )
        self._stop = threading.Event()
        self._frames_processed = 0
        self.frame_errors = 0
        self._thread_errors: list[BaseException] = []

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "PipelineRunner":
        if cfg.source.type != "synthetic":  # pragma: no cover - phase 2/3
            raise ValueError(f"unsupported source type {cfg.source.type!r}")
        # The source's trailing idle must outlast segment close + post-roll
        # or the final pass's miss evidence is truncated at flush.
        tail_s = (
            cfg.motion.quiet_frames / cfg.synthetic.fps + cfg.buffer.post_s + 0.5
        )
        source = SyntheticSource(cfg.synthetic, tail_s=tail_s)
        sinks: list[Sink] = []
        if cfg.sinks.console.enabled:
            sinks.append(ConsoleSink())
        if cfg.sinks.jsonl.enabled:
            sinks.append(JsonlSink(cfg.sinks.jsonl.path))
        if cfg.sinks.sqlite.enabled:
            sinks.append(SqliteSink(cfg.sinks.sqlite.path))
        return cls(cfg, source, sinks)

    def stop(self) -> None:
        """Request a graceful shutdown (drains queues before exiting)."""
        self._stop.set()

    # -- threads ---------------------------------------------------------------

    def _source_loop(self) -> None:
        live = self.source.live

        def _abort() -> bool:
            return self._stop.is_set() or bool(self._thread_errors)

        try:
            for frame in self.source.frames():
                if self._stop.is_set():
                    break
                if live:
                    self._frame_q.put(frame)
                elif not self._frame_q.put_blocking(frame, abort=_abort):
                    break
        except Exception as exc:
            self._thread_errors.append(exc)
            log.exception("source thread failed")
        finally:
            if live:
                self._frame_q.put(SENTINEL)
            else:
                # Blocking so the stream's tail frames are never displaced;
                # bail only if the pipeline thread already failed.
                self._frame_q.put_blocking(
                    SENTINEL, abort=lambda: bool(self._thread_errors)
                )
            self.source.close()

    def _process_frame(self, frame: Frame) -> None:
        self._buffer.append(frame)
        self._tracker.on_frame(frame)
        result, seg_event = self._gate.update(frame)
        if seg_event is not None and seg_event.kind is SegmentKind.OPEN:
            self._tracker.on_segment_open(seg_event)
        ctx = self._tracker.open_ctx
        if result.active and result.roi is not None and ctx is not None:
            decodes = self._engine.decode_frame(frame, result.roi, ctx)
            self._tracker.on_decode(decodes)
        if seg_event is not None and seg_event.kind is SegmentKind.CLOSE:
            self._tracker.on_segment_close(seg_event)

    def _pipeline_loop(self) -> None:
        try:
            while True:
                item = self._frame_q.get()
                if item is SENTINEL:
                    break
                try:
                    self._process_frame(item)
                except Exception:
                    # One bad frame must not abort the stream: the pallets
                    # behind it still need accounting.
                    self.frame_errors += 1
                    log.exception("frame %d failed; continuing", item.frame_index)
                self._frames_processed += 1
        except Exception as exc:
            self._thread_errors.append(exc)
            log.exception("pipeline thread failed")
        finally:
            # Always flush: pending misses must become events even when the
            # loop died, or open segments vanish without a trace.
            try:
                tail = self._gate.flush()
                if tail is not None:
                    self._tracker.on_segment_close(tail)
                self._tracker.flush()
            except Exception as exc:
                self._thread_errors.append(exc)
                log.exception("pipeline flush failed")

    # -- entry point -------------------------------------------------------------

    def run(self) -> RunSummary:
        """Run to source exhaustion (or :meth:`stop`), drain, and report."""
        self._bus.start()
        source_t = threading.Thread(
            target=self._source_loop, name="source", daemon=True
        )
        pipeline_t = threading.Thread(
            target=self._pipeline_loop, name="pipeline", daemon=True
        )
        source_t.start()
        pipeline_t.start()
        try:
            source_t.join()
            pipeline_t.join()
        finally:
            self._executor.shutdown(wait=True)
            self._bus.shutdown()
        if self._thread_errors:
            raise RuntimeError("pipeline thread failure") from self._thread_errors[0]

        reconciliation = None
        if isinstance(self.source, SyntheticSource):
            reconciliation = reconcile_truth(
                self.source.truth, self._collector.events, self._cfg.synthetic.fps
            )
        return RunSummary(
            frames=self._frames_processed,
            frames_dropped=self._frame_q.dropped,
            passes=self._tracker.passes_emitted,
            passes_merged=self._tracker.passes_merged,
            misses=self._tracker.misses_emitted,
            events_handled=self._bus.events_handled,
            sink_errors=self._bus.sink_errors,
            frame_errors=self.frame_errors,
            reconciliation=reconciliation,
        )

    @property
    def collected_events(self) -> list[Event]:
        """Events seen this run (for tests and the CLI report)."""
        return self._collector.events
