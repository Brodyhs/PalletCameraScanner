"""StationRunner: one PipelineRunner per camera + cross-camera business dedup.

Spec §4's required shape is literally "pipeline per camera": each arm keeps
its own MotionGate, decode budget, MetricsRegistry and watchdog, so one
camera's outage cannot pollute the other arm's stats — per-camera
independence IS the A/B experiment. The only genuinely cross-camera concern
is business-event dedup, which lives at the event layer
(:mod:`palletscan.events.dedup`):

    PipelineRunner(camA) ── bus A ── ForwardingSink ─┐
                                                      ├─► CrossCameraDeduper ─► business EventBus ─► sinks
    PipelineRunner(camB) ── bus B ── ForwardingSink ─┘

Per-camera runner internals (collector, metrics sink) are untouched, so
per-camera stats never dedupe. Business sinks hang off one business bus fed
by the deduper. An error-completion of either runner stops the others — a
half-running trial silently biases the experiment — while normal source
exhaustion does not.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from palletscan.app import (
    PipelineRunner,
    Reconciliation,
    RunSummary,
    _ListSink,
    build_sinks,
    reconcile_truth,
)
from palletscan.config import AppConfig
from palletscan.events.bus import EventBus
from palletscan.events.dedup import CrossCameraDeduper, ForwardingSink
from palletscan.sources.base import FrameSource
from palletscan.sources.factory import create_source
from palletscan.sources.synthetic import SyntheticSource
from palletscan.types import Event, PassEvent

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StationSummary:
    """Per-camera RunSummaries plus the business (deduped) accounting."""

    per_camera: dict[str, RunSummary]
    business: dict[str, int]  # deduper counters
    business_events_handled: int
    business_sink_errors: int
    collector_dropped: int
    reconciliation: Reconciliation | None = None

    @property
    def unaccounted(self) -> int:
        return len(self.reconciliation.unaccounted) if self.reconciliation else 0

    def format(self) -> str:
        lines = ["── station summary ──"]
        for source_id, summary in self.per_camera.items():
            lines.append(f"[{source_id}]")
            lines += ["  " + line for line in summary.format().splitlines()[1:]]
        b = self.business
        lines += [
            "[business]",
            f"  passes (deduped) : {b['passes_emitted']} "
            f"(+{b['cross_camera_merges']} cross-camera merges, "
            f"{b['repeats_suppressed']} repeats suppressed)",
            f"  misses forwarded : {b['misses_forwarded']}",
            f"  events handled   : {self.business_events_handled} "
            f"(sink errors {self.business_sink_errors})",
        ]
        if self.reconciliation is not None:
            r = self.reconciliation
            lines += [
                f"  truth passes     : {r.truth_passes}",
                f"    decoded        : {r.decoded} ({r.read_rate:.1%} read rate)",
                f"    missed (flagged): {r.missed}",
                f"    UNACCOUNTED    : {len(r.unaccounted)} {r.unaccounted or ''}",
            ]
        return "\n".join(lines)


def _business_view(events: list[Event]) -> list[Event]:
    """Distinct business events, max-revision-wins per event_id."""
    latest: dict[str, Event] = {}
    for ev in events:
        prev = latest.get(ev.event_id)
        if (
            prev is None
            or not isinstance(prev, PassEvent)
            or (isinstance(ev, PassEvent) and ev.revision >= prev.revision)
        ):
            latest[ev.event_id] = ev
    return list(latest.values())


class StationRunner:
    """Runs N per-camera pipelines into one deduped business event stream.

    Construct from a config whose ``source.cameras`` lists the camera ids,
    or inject ``sources`` directly (``synth --ab``, tests). Call
    :meth:`run` once.
    """

    def __init__(
        self, cfg: AppConfig, sources: Sequence[FrameSource] | None = None
    ) -> None:
        self._cfg = cfg
        if sources is None:
            if not cfg.source.cameras:
                raise ValueError("StationRunner requires source.cameras or sources")
            sources = [
                create_source(self._per_camera_cfg(camera_id))
                for camera_id in cfg.source.cameras
            ]
        ids = [s.source_id for s in sources]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate source ids in station: {ids}")

        self._collector = _ListSink()
        self.business_bus = EventBus(build_sinks(cfg) + [self._collector])
        self.deduper = CrossCameraDeduper(
            self.business_bus.publish, cfg.dedup.window_s
        )
        self.runners: dict[str, PipelineRunner] = {}
        for source in sources:
            self.runners[source.source_id] = PipelineRunner(
                self._per_camera_cfg(source.source_id),
                source,
                [ForwardingSink(self.deduper)],
            )
        self._summaries: dict[str, RunSummary] = {}
        self._errors: list[tuple[str, BaseException]] = []
        self._lock = threading.Lock()

    def _per_camera_cfg(self, source_id: str) -> AppConfig:
        """Per-runner config copy: single-camera selector + private evidence
        subdirectory (D5 — two runners sharing one evidence root race each
        other's prune and can silently eat a MissEvent)."""
        return self._cfg.model_copy(
            update={
                "source": self._cfg.source.model_copy(
                    update={"camera": source_id, "cameras": None}
                ),
                "evidence": self._cfg.evidence.model_copy(
                    update={"dir": self._cfg.evidence.dir / source_id}
                ),
            }
        )

    def stop(self) -> None:
        """Request a graceful stop of every per-camera runner."""
        for runner in self.runners.values():
            runner.stop()

    def _runner_thread(
        self, source_id: str, runner: PipelineRunner, stats_interval_s: float | None
    ) -> None:
        try:
            summary = runner.run(stats_interval_s=stats_interval_s)
            with self._lock:
                self._summaries[source_id] = summary
        except Exception as exc:
            with self._lock:
                self._errors.append((source_id, exc))
            log.exception("station runner %s failed; stopping the others", source_id)
            # A half-running trial silently biases the A/B comparison.
            self.stop()

    def run(self, stats_interval_s: float | None = None) -> StationSummary:
        """Run all per-camera pipelines to completion and report."""
        self.business_bus.start()
        threads = [
            threading.Thread(
                target=self._runner_thread,
                args=(source_id, runner, stats_interval_s),
                name=f"station-{source_id}",
                daemon=True,
            )
            for source_id, runner in self.runners.items()
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            # Per-camera buses have fully drained into the deduper by the
            # time runner.run() returns; now drain the business bus.
            self.business_bus.shutdown()
        if self._errors:
            source_id, exc = self._errors[0]
            # Chain from the runner error's cause so a WatchdogEscalation
            # survives to the CLI's exit-code mapping (cli inspects
            # exc.__cause__ -> exit 3).
            raise RuntimeError(
                f"station runner {source_id!r} failed"
            ) from (exc.__cause__ or exc)

        reconciliation = None
        synth_sources = [
            r.source for r in self.runners.values()
            if isinstance(r.source, SyntheticSource)
        ]
        if len(synth_sources) == len(self.runners) and synth_sources:
            # Reconcile against DISTINCT business events (max-revision rows):
            # truth is per-pallet, and same-seed sources share one schedule.
            fps = synth_sources[0].nominal_fps or 30.0
            reconciliation = reconcile_truth(
                synth_sources[0].truth,
                _business_view(self._collector.events),
                fps,
            )
        return StationSummary(
            per_camera=dict(self._summaries),
            business=self.deduper.stats(),
            business_events_handled=self.business_bus.events_handled,
            business_sink_errors=self.business_bus.sink_errors,
            collector_dropped=self._collector.dropped,
            reconciliation=reconciliation,
        )
