"""MetricsRegistry: percentiles, windows, snapshot contract, pipeline wiring."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from palletscan.app import PipelineRunner
from palletscan.config import AppConfig, DecodeConfig, MetricsConfig
from palletscan.metrics import (
    MetricsRegistry,
    _SecondBuckets,
    _SourceTimeWindow,
    percentile,
)
from palletscan.pipeline.decode_engine import DecodeEngine, PassDecodeContext
from palletscan.types import Frame, Roi

#: snapshot() is the Phase 4 /stats.json contract — changing this set is an
#: API change, not a refactor.
SNAPSHOT_KEYS = {
    "uptime_s",
    "fps",
    "frames",
    "queues",
    "decode",
    "passes",
    "misses",
    "source",  # Phase 3 watchdog counters (approved API extension)
    "read_rate_1h",
    "events",
    "outbox",
}


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _registry(clock: FakeClock, **cfg_overrides) -> MetricsRegistry:
    return MetricsRegistry(MetricsConfig(**cfg_overrides), clock=clock)


# -- percentile -----------------------------------------------------------------


def test_percentile_empty_is_none() -> None:
    assert percentile([], 0.5) is None


def test_percentile_single_and_known_distribution() -> None:
    assert percentile([7.0], 0.5) == 7.0
    samples = sorted(float(i) for i in range(1, 101))  # 1..100
    assert percentile(samples, 0.50) == pytest.approx(50.0, abs=1.0)
    assert percentile(samples, 0.95) == pytest.approx(95.0, abs=1.0)


# -- _SecondBuckets ----------------------------------------------------------------


def test_second_buckets_steady_rate() -> None:
    b = _SecondBuckets(window_s=10)
    for sec in range(20):
        for k in range(30):
            b.add(100.0 + sec + k / 30.0)
    # 30/s for 20 s: the rate must be unbiased (the window's partial
    # current second is divided by its actual coverage, not the full
    # window) just before a second boundary...
    assert b.rate(119.99, elapsed_s=20.0) == pytest.approx(30.0, rel=0.02)
    # ...and just after one, when only the events that have actually
    # happened are in the current bucket.
    b2 = _SecondBuckets(window_s=10)
    t = 100.0
    while t <= 119.01:
        b2.add(t)
        t += 1.0 / 30.0
    assert b2.rate(119.01, elapsed_s=19.01) == pytest.approx(30.0, rel=0.05)


def test_second_buckets_expire_old_counts() -> None:
    b = _SecondBuckets(window_s=5)
    for k in range(50):
        b.add(100.0 + k / 50.0)  # burst within one second
    assert b.rate(100.5, elapsed_s=10.0) > 0
    assert b.rate(120.0, elapsed_s=30.0) == 0.0  # burst aged out


def test_second_buckets_early_run_uses_elapsed() -> None:
    b = _SecondBuckets(window_s=60)
    for k in range(60):
        b.add(100.0 + k / 30.0)  # 2 s of 30 fps
    # Dividing by the full 60 s window would report 1 fps; elapsed corrects it.
    assert b.rate(102.0, elapsed_s=2.0) == pytest.approx(30.0, rel=0.1)


# -- _SourceTimeWindow -------------------------------------------------------------


def test_source_time_window_counts_and_expires() -> None:
    w = _SourceTimeWindow(window_s=100.0)
    for ts in (0.0, 10.0, 50.0, 99.0):
        w.add(ts)
    assert w.count_since(99.0) == 4
    assert w.count_since(150.0) == 2  # 0 and 10 fell out of [50, 150]


# -- MetricsRegistry ------------------------------------------------------------


def test_fps_from_record_frame() -> None:
    clock = FakeClock()
    m = _registry(clock, window_s=10.0)
    for i in range(300):  # 10 s of 30 fps
        m.record_frame(source_ts=i / 30.0)
        clock.advance(1 / 30.0)
    snap = m.snapshot()
    assert snap["fps"] == pytest.approx(30.0, rel=0.05)


def test_latency_reservoir_bounded_and_percentiles() -> None:
    clock = FakeClock()
    m = _registry(clock, latency_samples=100)
    for i in range(1000):
        m.record_decode_wall_ms(float(i))
    snap = m.snapshot()
    assert snap["decode"]["samples"] == 100
    # Only the most recent 100 samples (900..999) remain.
    assert snap["decode"]["p50_ms"] == pytest.approx(950.0, abs=5.0)
    assert snap["decode"]["p95_ms"] == pytest.approx(995.0, abs=5.0)


def test_no_decode_samples_yields_none_percentiles() -> None:
    m = _registry(FakeClock())
    snap = m.snapshot()
    assert snap["decode"]["p50_ms"] is None
    assert snap["decode"]["p95_ms"] is None


def test_passes_per_hour_and_read_rate() -> None:
    clock = FakeClock()
    m = _registry(clock)
    # 10 minutes of source time, 30 passes + 10 misses inside it.
    for i in range(40):
        ts = i * 15.0  # 0..585 s
        if i % 4 == 3:
            m.record_miss(ts)
        else:
            m.record_pass(ts)
    m.record_frame(source_ts=0.0)
    m.record_frame(source_ts=600.0)
    snap = m.snapshot()
    assert snap["read_rate_1h"] == pytest.approx(0.75)
    assert snap["passes"]["per_hour"] == pytest.approx(30 * 3600 / 600, rel=0.05)


def test_read_rate_none_without_events() -> None:
    m = _registry(FakeClock())
    m.record_frame(source_ts=0.0)
    assert m.snapshot()["read_rate_1h"] is None
    assert m.snapshot()["passes"]["per_hour"] == 0.0


def test_rates_anchor_on_frame_clock_and_decay_when_idle() -> None:
    m = _registry(FakeClock())
    m.record_frame(source_ts=0.0)
    m.record_pass(10.0)
    m.record_frame(source_ts=20.0)
    assert m.snapshot()["passes"]["per_hour"] > 0
    # Source clock advances 2h with no events: the pass leaves the window
    # even though it is still the most recent event.
    m.record_frame(source_ts=7200.0)
    snap = m.snapshot()
    assert snap["passes"]["per_hour"] == 0.0
    assert snap["read_rate_1h"] is None


def test_register_gauges_rejects_unknown_names() -> None:
    m = _registry(FakeClock())
    with pytest.raises(ValueError, match="no_such_gauge"):
        m.register_gauges(no_such_gauge=lambda: 1)


def test_gauges_and_queues_sampled_at_snapshot() -> None:
    m = _registry(FakeClock())
    state = {"frames": 0, "depth": 3}
    m.register_gauges(frames_processed=lambda: state["frames"])
    m.register_queue("frames", lambda: state["depth"])
    state["frames"] = 17
    snap = m.snapshot()
    assert snap["frames"]["processed"] == 17
    assert snap["queues"]["frames"] == 3


def test_outbox_probe_optional() -> None:
    m = _registry(FakeClock())
    assert m.snapshot()["outbox"] is None
    m.set_outbox_probe(lambda: {"depth": 5, "oldest_age_s": 12.0})
    assert m.snapshot()["outbox"] == {"depth": 5, "oldest_age_s": 12.0}


def test_snapshot_key_contract() -> None:
    snap = _registry(FakeClock()).snapshot()
    assert set(snap) == SNAPSHOT_KEYS
    assert set(snap["decode"]) == {
        "p50_ms",
        "p95_ms",
        "samples",
        "pyzbar_calls",
        "dmtx_calls",
        "fallback_calls",
        "budget_overruns",
    }
    assert set(snap["passes"]) == {"emitted", "merged", "per_hour"}
    assert set(snap["frames"]) == {"processed", "dropped", "errors"}
    assert set(snap["events"]) == {"handled", "sink_errors"}


# -- DecodeEngine observer ------------------------------------------------------


def test_engine_records_wall_time_for_failed_attempts() -> None:
    samples: list[float] = []
    with ThreadPoolExecutor(max_workers=1) as ex:
        engine = DecodeEngine(DecodeConfig(), ex, observe_wall_ms=samples.append)
        blank = Frame(
            image=np.full((120, 160), 128, np.uint8),
            ts=0.0,
            frame_index=0,
            source_id="t",
        )
        results = engine.decode_frame(blank, Roi(0, 0, 160, 120), PassDecodeContext())
    assert results == []  # nothing decodable in a flat frame
    assert len(samples) == 1 and samples[0] >= 0.0


# -- pipeline integration -----------------------------------------------------


def test_snapshot_sane_after_full_run(fast_synth_config: AppConfig) -> None:
    runner = PipelineRunner.from_config(fast_synth_config)
    summary = runner.run()
    snap = summary.metrics
    assert snap is not None
    assert set(snap) == SNAPSHOT_KEYS
    assert snap["frames"]["processed"] == summary.frames
    assert snap["passes"]["emitted"] == summary.passes == 3
    assert snap["misses"]["emitted"] == 0
    assert snap["read_rate_1h"] == pytest.approx(1.0)
    assert snap["passes"]["per_hour"] > 0
    assert snap["decode"]["samples"] > 0
    assert snap["decode"]["p50_ms"] is not None
    assert snap["decode"]["p95_ms"] >= snap["decode"]["p50_ms"]
    assert snap["fps"] > 0
    assert snap["events"]["handled"] == summary.events_handled
    assert snap["queues"] == {"frames": 0, "events": 0}
    assert snap["uptime_s"] > 0
