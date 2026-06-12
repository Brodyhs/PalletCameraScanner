"""tools/measure_cpu.py summary math + tools/demo.py wiring (Phase 5)."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from palletscan.config import AppConfig, load_config
from tools import demo, measure_cpu

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_summarize_avg_p95_max() -> None:
    samples = [float(v) for v in range(1, 101)]  # 1..100
    s = measure_cpu.summarize("x", samples)
    assert s.samples == 100
    assert s.avg_pct == pytest.approx(50.5)
    assert s.p95_pct == pytest.approx(95.0)
    assert s.max_pct == pytest.approx(100.0)


def test_summarize_empty_is_zero() -> None:
    s = measure_cpu.summarize("x", [])
    assert (s.samples, s.avg_pct, s.p95_pct, s.max_pct) == (0, 0.0, 0.0, 0.0)


def test_sum_per_sample_truncates_to_shortest() -> None:
    a = [10.0, 20.0, 30.0]
    b = [1.0, 2.0]
    assert measure_cpu.sum_per_sample([a, b]) == [11.0, 22.0]
    assert measure_cpu.sum_per_sample([]) == []


def test_burst_config_tightens_idle_gaps_only() -> None:
    cfg = AppConfig()
    burst = measure_cpu.burst_config(cfg, passes=60)
    assert burst.synthetic.idle_s_range == measure_cpu.BURST_IDLE_S
    assert burst.synthetic.num_passes == 60
    # the decodability envelope is untouched — burst means cadence, not blur
    assert burst.synthetic.speed_mph_range == cfg.synthetic.speed_mph_range
    assert burst.synthetic.px_per_module_range == cfg.synthetic.px_per_module_range
    assert cfg.synthetic.idle_s_range == (0.3, 1.5), "input not mutated"


def test_render_report_normalizes_to_4_core_budget() -> None:
    s = measure_cpu.summarize("replay (1 cam)", [100.0, 100.0, 200.0])
    md = measure_cpu.render_report("baseline", [s])
    assert "avg /4-core" in md
    # p95 of 3 samples is the index-1 order statistic: 100.0
    assert "| replay (1 cam) | 3 | 133.3 | 100.0 | 200.0 | 33.3% | 25.0% |" in md


# -- demo ----------------------------------------------------------------------


def test_demo_yaml_is_strict_valid_realtime_ab_ready() -> None:
    cfg = load_config(REPO_ROOT / "config" / "demo.yaml")
    assert cfg.synthetic.realtime is True, "the demo must be sleep-paced"
    assert cfg.web.enabled is True
    assert cfg.web.port != 0, "the readiness poll needs a fixed port"
    assert cfg.sinks.console.enabled is False
    assert cfg.sinks.sqlite.enabled is True, "the dashboard reads SQLite"


def test_wait_until_ready_polls_until_probe_true() -> None:
    clock = {"now": 0.0}
    probes: list[float] = []

    def probe() -> bool:
        probes.append(clock["now"])
        return len(probes) >= 3

    ok = demo.wait_until_ready(
        probe,
        timeout_s=10.0,
        sleep=lambda s: clock.__setitem__("now", clock["now"] + s),
        clock=lambda: clock["now"],
    )
    assert ok and len(probes) == 3


def test_wait_until_ready_false_on_child_death_and_timeout() -> None:
    clock = {"now": 0.0}

    def tick(s: float) -> None:
        clock["now"] += s

    assert not demo.wait_until_ready(
        lambda: False,
        timeout_s=2.0,
        sleep=tick,
        clock=lambda: clock["now"],
    ), "timeout must return False"
    assert not demo.wait_until_ready(
        lambda: True,  # never reached: the child is gone
        timeout_s=2.0,
        child_alive=lambda: False,
        sleep=tick,
        clock=lambda: clock["now"],
    ), "a dead child must return False immediately"


@pytest.mark.acceptance
def test_demo_smoke_end_to_end(tmp_path: Path) -> None:
    """demo.py --no-browser --max-seconds 20: child spawns, the stats
    endpoint serves, shutdown is clean (exit 0 with the drain summary)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    raw = yaml.safe_load(
        (REPO_ROOT / "config" / "demo.yaml").read_text(encoding="utf-8")
    )
    raw["web"]["port"] = port
    cfg = tmp_path / "demo.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "demo.py"),
            "--no-browser",
            "--max-seconds",
            "20",
            "--config",
            str(cfg),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, f"stdout:\n{out.stdout}\nstderr:\n{out.stderr}"
    assert "dashboard ready at" in out.stdout
    assert "station summary" in out.stdout, "the drain must print the summary"
    assert "UNACCOUNTED    : 0" in out.stdout
