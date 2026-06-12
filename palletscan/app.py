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
from palletscan.events.http_sink import HttpSink
from palletscan.events.sinks import ConsoleSink, JsonlSink, Sink, SqliteSink
from palletscan.metrics import MetricsRegistry
from palletscan.pipeline.decode_engine import DecodeEngine
from palletscan.pipeline.motion_gate import MotionGate
from palletscan.pipeline.pass_tracker import PassTracker
from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.reliability.queues import SENTINEL, DroppingQueue
from palletscan.reliability.watchdog import WatchdogSource
from palletscan.sources.base import FrameSource
from palletscan.sources.factory import create_source
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
    metrics: dict | None = None

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
        if self.metrics is not None:
            d = self.metrics["decode"]
            if d["p50_ms"] is not None:
                lines.append(
                    f"decode wall time : p50 {d['p50_ms']:.1f} ms / "
                    f"p95 {d['p95_ms']:.1f} ms ({d['samples']} samples)"
                )
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
    """In-memory sink used for run summaries and tests (capped).

    Overflow is counted, never silent: consumers doing exact accounting
    (the soak harness) must check :attr:`dropped` before trusting
    :attr:`events` as the complete record.
    """

    def __init__(self, cap: int = _EVENT_COLLECT_CAP) -> None:
        self.events: list[Event] = []
        self.dropped = 0
        self._cap = cap

    def handle(self, event: Event) -> None:
        if len(self.events) < self._cap:
            self.events.append(event)
        else:
            self.dropped += 1
            if self.dropped == 1:
                log.warning(
                    "event collector full (%d); further events are counted "
                    "but not retained in memory",
                    self._cap,
                )


class _MetricsSink(Sink):
    """Feeds pass/miss source timestamps into the metrics rolling windows."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self._metrics = metrics

    def handle(self, event: Event) -> None:
        if isinstance(event, PassEvent):
            self._metrics.record_pass(event.last_seen_ts)
        elif isinstance(event, MissEvent):
            self._metrics.record_miss(event.end_ts)


class PipelineRunner:
    """One source's pipeline. Construct via :meth:`from_config`, call
    :meth:`run` once."""

    def __init__(self, cfg: AppConfig, source: FrameSource, sinks: list[Sink]) -> None:
        self._cfg = cfg
        self.source = source
        self.metrics = MetricsRegistry(cfg.metrics)
        self._collector = _ListSink()
        self._bus = EventBus(sinks + [self._collector, _MetricsSink(self.metrics)])
        self._frame_q = DroppingQueue(maxsize=64)
        self._executor: Executor = (
            ThreadPoolExecutor(
                max_workers=cfg.decode.workers, thread_name_prefix="decode"
            )
            if cfg.decode.executor is ExecutorKind.THREAD
            else ProcessPoolExecutor(max_workers=cfg.decode.workers)
        )
        self._gate = MotionGate(cfg.motion, source.source_id)
        self._engine = DecodeEngine(
            cfg.decode, self._executor, observe_wall_ms=self.metrics.record_decode_wall_ms
        )
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
        # Set only when the pipeline (consumer) thread dies: the sentinel
        # put must keep blocking through a merely-slow consumer, and the
        # source's own failure must NOT abort it (that exception also lands
        # in _thread_errors, which is why the sentinel cannot key off it).
        self._pipeline_dead = threading.Event()
        self._frames_processed = 0
        self.frame_errors = 0
        self._thread_errors: list[BaseException] = []
        # Existing component counters stay the source of truth; the registry
        # reads them lazily at snapshot time.
        self.metrics.register_gauges(
            frames_processed=lambda: self._frames_processed,
            frames_dropped=lambda: self._frame_q.dropped,
            frame_errors=lambda: self.frame_errors,
            passes_emitted=lambda: self._tracker.passes_emitted,
            passes_merged=lambda: self._tracker.passes_merged,
            misses_emitted=lambda: self._tracker.misses_emitted,
            events_handled=lambda: self._bus.events_handled,
            sink_errors=lambda: self._bus.sink_errors,
            pyzbar_calls=lambda: self._engine.counters.pyzbar_calls,
            dmtx_calls=lambda: self._engine.counters.dmtx_calls,
            fallback_calls=lambda: self._engine.counters.fallback_calls,
            budget_overruns=lambda: self._engine.counters.budget_overruns,
        )
        # Watchdog counters stay the single source of truth (same lazy-gauge
        # pattern); non-camera runs report zeros in the "source" section.
        if isinstance(source, WatchdogSource):
            self.metrics.register_gauges(
                source_stalls=lambda: source.stalls_detected,
                source_reconnects=lambda: source.reconnects,
                source_reopen_failures=lambda: source.reopen_failures,
                source_zombie_readers=lambda: source.zombie_readers,
            )
        self.metrics.register_queue("frames", self._frame_q.qsize)
        self.metrics.register_queue("events", self._bus.queue.qsize)
        for sink in sinks:
            if isinstance(sink, HttpSink):
                self.metrics.set_outbox_probe(sink.outbox_stats)

    @classmethod
    def from_config(
        cls, cfg: AppConfig, source: FrameSource | None = None
    ) -> "PipelineRunner":
        """Build a runner with config-driven sinks. ``source`` overrides the
        config-selected source (e.g. a FlakySource-wrapped one in soak)."""
        if source is None:
            source = create_source(cfg)
        sinks: list[Sink] = []
        if cfg.sinks.console.enabled:
            sinks.append(ConsoleSink())
        if cfg.sinks.jsonl.enabled:
            sinks.append(JsonlSink(cfg.sinks.jsonl.path))
        if cfg.sinks.sqlite.enabled:
            sinks.append(SqliteSink(cfg.sinks.sqlite.path))
        if cfg.sinks.http.enabled:
            sinks.append(HttpSink(cfg.sinks.http))
        return cls(cfg, source, sinks)

    def stop(self) -> None:
        """Request a graceful shutdown (drains queues before exiting)."""
        self._stop.set()
        # A watchdog-wrapped source mid-outage never yields a frame, so the
        # source thread cannot observe _stop between frames; closing the
        # wrapper (idempotent, thread-safe) interrupts its stall-wait and
        # backoff so shutdown stays prompt even with a dead camera.
        if isinstance(self.source, WatchdogSource):
            self.source.close()

    # -- threads ---------------------------------------------------------------

    def _source_loop(self) -> None:
        live = self.source.live

        def _abort() -> bool:
            return self._stop.is_set() or self._pipeline_dead.is_set()

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
                # bail only if the pipeline thread (the consumer) is dead —
                # losing this sentinel deadlocks the pipeline in get().
                self._frame_q.put_blocking(
                    SENTINEL, abort=self._pipeline_dead.is_set
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
                self.metrics.record_frame(item.ts)
                try:
                    self._process_frame(item)
                except Exception:
                    # One bad frame must not abort the stream: the pallets
                    # behind it still need accounting.
                    self.frame_errors += 1
                    log.exception("frame %d failed; continuing", item.frame_index)
                self._frames_processed += 1
        except Exception as exc:
            self._pipeline_dead.set()
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

    def _stats_loop(self, interval_s: float, stop: threading.Event) -> None:
        while not stop.wait(interval_s):
            log.info("metrics", extra={"stats": self.metrics.snapshot()})

    def run(self, stats_interval_s: float | None = None) -> RunSummary:
        """Run to source exhaustion (or :meth:`stop`), drain, and report.

        ``stats_interval_s`` adds a periodic structured-log line with the
        metrics snapshot (the ``--stats-interval`` CLI flag).
        """
        self._bus.start()
        source_t = threading.Thread(
            target=self._source_loop, name="source", daemon=True
        )
        pipeline_t = threading.Thread(
            target=self._pipeline_loop, name="pipeline", daemon=True
        )
        stats_stop = threading.Event()
        if stats_interval_s is not None and stats_interval_s > 0:
            threading.Thread(
                target=self._stats_loop,
                args=(stats_interval_s, stats_stop),
                name="stats",
                daemon=True,
            ).start()
        source_t.start()
        pipeline_t.start()
        try:
            source_t.join()
            pipeline_t.join()
        finally:
            stats_stop.set()
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
            metrics=self.metrics.snapshot(),
        )

    @property
    def collected_events(self) -> list[Event]:
        """Events seen this run (for tests and the CLI report)."""
        return self._collector.events

    @property
    def collected_events_dropped(self) -> int:
        """Events the in-memory collector could not retain (cap overflow)."""
        return self._collector.dropped
