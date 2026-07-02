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


# -- REVIEW_SYSTEM_0c30c77 findings b5 and b6 ----------------------------------


def test_port_in_use_detects_bound_socket() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b5: the readiness probe accepts any
    HTTP 200, so a pre-existing server on the demo port (the LIVE station
    dashboard) made the demo declare itself ready against real trial data.
    The preflight bind-check is what closes that path."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        assert demo.port_in_use("127.0.0.1", port) is True
    assert demo.port_in_use("127.0.0.1", port) is False


def test_demo_port_avoids_production_dashboard_default() -> None:
    """Finding b5, second arm: config/demo.yaml pinned 8000 — the
    production dashboard default — making the live-station collision the
    factory-box default."""
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "demo.yaml")
    assert cfg.web.port != 8000


def test_demo_refuses_when_port_already_bound(tmp_path: Path, capsys) -> None:
    """Finding b5: the demo must refuse up front, before spawning a child
    whose port-bind exit 2 gets buried under a false 'ready'."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        cfg = tmp_path / "demo.yaml"
        cfg.write_text(
            f"web: {{enabled: true, port: {port}}}\n", encoding="utf-8"
        )
        rc = demo.main(
            ["--config", str(cfg), "--no-browser", "--data-dir", str(tmp_path / "d")]
        )
    err = capsys.readouterr().err
    assert rc == 2
    assert "already serving" in err


class _StubChild:
    """Popen-shaped recorder for _stop's signalling decisions."""

    def __init__(self) -> None:
        self.signals: list = []
        self.terminates = 0
        self.waits = 0
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        self.waits += 1
        return 0

    def send_signal(self, sig) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminates += 1

    def kill(self) -> None:  # pragma: no cover - wedged-child path
        self._alive = False


def test_stop_skips_second_signal_for_already_interrupted_child() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b6 (repro: Ctrl-C outside the wait
    loop reached the finally, which sent a SECOND stop signal to a child
    whose first-signal handler had already restored SIG_DFL — hard kill
    mid-drain). An already-signalled child is waited out, not re-signalled."""
    child = _StubChild()
    assert demo._stop(child, already_signaled=True) == 0
    assert child.signals == [] and child.terminates == 0
    assert child.waits >= 1

    fresh = _StubChild()
    demo._stop(fresh)  # the smoke-mode stop still signals once
    assert (len(fresh.signals) + fresh.terminates) == 1


class _CtrlCWaitChild:
    """Popen-shaped child for the interactive wait loop: the first wait()
    raises KeyboardInterrupt (the operator's Ctrl-C reaching the PARENT).
    Because it was spawned with CREATE_NEW_PROCESS_GROUP the console event
    never reaches IT, so it exits only once a stop channel is actually
    delivered — a bare re-wait() models the pre-fix hang."""

    def __init__(self) -> None:
        self.signals: list = []
        self.stop_delivered = False
        self._interrupts = 1
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        if self._interrupts:
            self._interrupts -= 1
            raise KeyboardInterrupt
        if not self.stop_delivered:
            raise RuntimeError(
                "wait() re-entered with no stop delivered: the new-process-"
                "group child never saw the console Ctrl-C and runs forever"
            )
        self._alive = False
        return 0

    def send_signal(self, sig) -> None:
        self.signals.append(sig)
        self.stop_delivered = True

    def kill(self) -> None:  # pragma: no cover - wedged-child path
        self._alive = False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console Ctrl-C semantics")
def test_windows_ctrl_c_in_wait_loop_stops_the_new_process_group_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review of REVIEW_bringup_4d95b67 (tools/demo.py): spawning the
    child with CREATE_NEW_PROCESS_GROUP implicitly disables console Ctrl-C
    delivery to it, but the interactive Ctrl-C paths still assumed 'the
    event reached the child' and just re-wait()ed — so an interactive
    Windows demo could no longer be stopped with Ctrl-C. A parent-side
    KeyboardInterrupt must forward the stop (stop-file latch + CTRL_BREAK),
    not wait for a drain that never starts."""
    import signal

    cfg = tmp_path / "demo.yaml"
    cfg.write_text("web: {enabled: true, port: 18999}\n", encoding="utf-8")
    child = _CtrlCWaitChild()
    monkeypatch.setattr(demo.subprocess, "Popen", lambda cmd, **kw: child)
    monkeypatch.setattr(demo, "port_in_use", lambda host, port: False)
    monkeypatch.setattr(demo, "_stats_ok", lambda url: True)
    rc = demo.main(
        ["--config", str(cfg), "--no-browser", "--data-dir", str(tmp_path / "d")]
    )
    assert rc == 0
    # The durable channel (works without a console) AND the accelerator both
    # went out — already_signaled=True would have skipped both.
    assert (tmp_path / "d" / "palletscan.stop").exists()
    assert signal.CTRL_BREAK_EVENT in child.signals
