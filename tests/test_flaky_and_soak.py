"""FlakySource injection semantics + the short soak variant.

The soak harness itself lives in tools/soak.py (psutil is a [dev] extra);
the soak_short test imports it and asserts the same invariants as the full
2h run: flat RSS, zero unhandled exceptions, injected failure -> restart
gap < 10 s with zero event loss (truth reconciliation + outbox).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from palletscan.app import PipelineRunner
from palletscan.config import AppConfig
from palletscan.reliability.flaky import FlakySource, InjectedFailure
from palletscan.sources.factory import create_source
from tools import soak


def test_flaky_delegates_and_raises_on_cue(fast_synth_config: AppConfig) -> None:
    inner = create_source(fast_synth_config)
    src = FlakySource(inner, raise_at=10)
    assert src.source_id == inner.source_id
    assert src.nominal_fps == inner.nominal_fps
    assert src.live == inner.live
    got = []
    with pytest.raises(InjectedFailure):
        for f in src.frames():
            got.append(f.frame_index)
    assert got == list(range(10))


def test_flaky_stall_mode_only_delays(fast_synth_config: AppConfig) -> None:
    src = FlakySource(create_source(fast_synth_config), stall_at=5, stall_s=0.2)
    frames = list(src.frames())
    assert len(frames) > 100  # full run survived the stall
    assert src.frames_emitted == len(frames)


def test_injected_failure_dies_through_flush_path(
    fast_synth_config: AppConfig,
) -> None:
    """Crash-only: the run must raise (supervisor's signal to restart) AND
    still account for the interrupted segment via the flush-in-finally."""
    source = FlakySource(create_source(fast_synth_config), raise_at=40)
    runner = PipelineRunner.from_config(fast_synth_config, source=source)
    with pytest.raises(RuntimeError) as exc_info:
        runner.run()
    assert isinstance(exc_info.value.__cause__, InjectedFailure)
    # Frame 40 is mid-first-pass (idle is ~12-18 frames): the open motion
    # segment must have been flushed into a pass or miss event, not lost.
    assert len(runner.collected_events) >= 1
    jsonl = fast_synth_config.sinks.jsonl.path
    assert jsonl.exists() and jsonl.read_text().strip()


def test_source_failure_with_full_queue_and_slow_pipeline_terminates(
    fast_synth_config: AppConfig,
) -> None:
    """Regression: the end-of-stream sentinel must keep blocking through a
    merely-slow pipeline after a source failure. The source's own exception
    used to satisfy the sentinel's abort predicate, so a single 0.5s
    full-queue window silently dropped the sentinel and the pipeline thread
    blocked in get() forever."""
    import threading
    import time as _time

    from palletscan.reliability.queues import DroppingQueue

    source = FlakySource(create_source(fast_synth_config), raise_at=8)
    runner = PipelineRunner.from_config(fast_synth_config, source=source)
    runner._frame_q = DroppingQueue(maxsize=2)  # keep the queue pinned full
    runner._process_frame = lambda frame: _time.sleep(0.6)  # type: ignore[method-assign]

    outcome: list[BaseException | None] = []

    def _run() -> None:
        try:
            runner.run()
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=30.0)
    assert not t.is_alive(), "run() wedged after source failure (lost sentinel)"
    assert isinstance(outcome[0], RuntimeError)
    assert isinstance(outcome[0].__cause__, InjectedFailure)


def test_list_sink_overflow_is_counted(fast_synth_config: AppConfig) -> None:
    from palletscan.app import _ListSink

    sink = _ListSink(cap=3)
    for i in range(5):
        sink.handle(object())  # type: ignore[arg-type]
    assert len(sink.events) == 3
    assert sink.dropped == 2


def test_analyze_rss_flags_growth() -> None:
    flat = [(float(t), 100.0 + (t % 3) * 0.1) for t in range(0, 120, 2)]
    v = soak.analyze_rss(flat, warmup_s=20.0, max_slope_mb_per_min=1.0)
    assert v.enough_samples and v.problems == []
    leaking = [(float(t), 100.0 + t * 0.5) for t in range(0, 120, 2)]  # 30 MB/min
    v = soak.analyze_rss(leaking, warmup_s=20.0, max_slope_mb_per_min=1.0)
    assert v.problems, "a 30 MB/min ramp must be flagged"
    v = soak.analyze_rss(leaking[:3], warmup_s=0.0, max_slope_mb_per_min=1.0)
    assert not v.enough_samples


@pytest.mark.soak_short
def test_short_soak_invariants(tmp_path: Path) -> None:
    """~2.5-minute synthetic soak with a failure injected every 30 source-
    seconds — the CI-sized version of the 2h manual run."""
    cfg_path = tmp_path / "soak.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "synthetic": {
                    "width": 640,
                    "height": 360,
                    "speed_mph_range": [2.0, 8.0],
                    "angle_deg_range": [0.0, 25.0],
                    "contrast_range": [0.55, 1.0],
                    "occlusion_max_frac": 0.1,
                },
                "logging": {"level": "WARNING"},
            }
        )
    )
    args = soak.parse_args(
        [
            "--minutes", "2.5",
            "--mode", "synthetic",
            "--inject-every-s", "30",
            "--config", str(cfg_path),
            "--data-dir", str(tmp_path / "data"),
            "--seed", "7",
            "--rss-interval-s", "1",
            "--warmup-s", "45",
            # A ~100 s fit window over-extrapolates settling noise (a few
            # MB of gc/allocator drift reads as MB/min, and varies run to
            # run with machine load); the strict 1.0 default is for the 2h
            # run, where the window dilutes that noise ~80x. 8.0 still
            # catches leak-class slopes (the pre-fix restart churn measured
            # +323 MB/min) and the 1.3x final/baseline ratio stays the
            # absolute-growth guard.
            "--max-slope-mb-per-min", "8.0",
            "--stats-interval", "30",
        ]
    )
    report = soak.run_soak(args)
    print(report.format())
    assert report.ok, report.format()
    assert report.restarts >= 2, "expected multiple injected restarts"
    assert report.max_gap_s < 10.0
    assert sum(s.frames for s in report.segments) > 1000
    truth_total = sum(
        s.reconciliation.truth_passes
        for s in report.segments
        if s.reconciliation is not None
    )
    assert truth_total > 10, "soak barely exercised the pipeline"
