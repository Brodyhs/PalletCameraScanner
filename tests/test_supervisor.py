"""Supervisor restart/backoff/stop semantics (Phase 5, D4–D7).

Two layers: fake spawn/clock/sleep for the policy logic (cross-platform,
no real processes), then real ``sys.executable -c`` children for the spawn
path and exit-code fidelity.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from palletscan.cli import main
from palletscan.reliability.instance_lock import hold_instance_lock
from palletscan.reliability.supervisor import (
    Supervisor,
    SupervisorOptions,
    _reason_for,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: list = []  # callables fired with the clock each sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in self.on_sleep:
            hook(self)


class FakeChild:
    """Scripted child: exits with ``code`` after ``runtime`` fake-seconds,
    or immediately once signalled (graceful drain)."""

    def __init__(
        self, clock: FakeClock, runtime: float, code: int, drain_code: int = 0
    ) -> None:
        self._clock = clock
        self._deadline = clock.now + runtime
        self._code = code
        self._drain_code = drain_code
        self.signals: list[int] = []
        self.kills = 0
        self.pid = 4242

    def poll(self) -> int | None:
        if self.signals:
            return self._drain_code
        return self._code if self._clock.now >= self._deadline else None

    def wait(self, timeout: float | None = None) -> int:
        code = self.poll()
        if code is not None:
            return code
        if timeout is not None and not math.isinf(self._deadline):
            self._clock.sleep(min(timeout, self._deadline - self._clock.now))
        code = self.poll()
        if code is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)
        return code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.kills += 1
        self._deadline = self._clock.now
        self._code = -9


def _supervisor(
    tmp_path: Path, children: list[FakeChild], clock: FakeClock, **opt_overrides
) -> tuple[Supervisor, list[list[str]]]:
    spawned: list[list[str]] = []
    queue = list(children)

    def spawn(cmd: list[str]) -> FakeChild:
        spawned.append(cmd)
        return queue.pop(0)

    opts = SupervisorOptions(
        data_dir=tmp_path,
        command=["python", "-m", "palletscan", "run"],
        **opt_overrides,
    )
    return Supervisor(opts, spawn=spawn, clock=clock, sleep=clock.sleep), spawned


def _restart_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "logs" / "restarts.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_restarts_on_nonzero_then_ends_on_clean_exit(tmp_path: Path) -> None:
    clock = FakeClock()
    children = [
        FakeChild(clock, runtime=10.0, code=1),
        FakeChild(clock, runtime=10.0, code=0),
    ]
    sup, spawned = _supervisor(tmp_path, children, clock)
    assert sup.run() == 0
    assert len(spawned) == 2
    lines = _restart_lines(tmp_path)
    assert [ln["exit_code"] for ln in lines] == [1, 0]
    assert lines[0]["reason"] == "software-failure"
    assert lines[0]["delay_s"] == 5.0
    assert lines[0]["runtime_s"] == pytest.approx(10.0)
    assert lines[1]["reason"] == "clean-exit"
    assert all("ts" in ln for ln in lines)


def test_escalations_countable_by_exit_code(tmp_path: Path) -> None:
    """The RUNBOOK one-liner contract: exit-3 lines (USB stack wedged) are
    distinguishable from exit-1 crashes by a field filter, no log diving."""
    clock = FakeClock()
    children = [
        FakeChild(clock, runtime=120.0, code=3),
        FakeChild(clock, runtime=120.0, code=1),
        FakeChild(clock, runtime=120.0, code=3),
        FakeChild(clock, runtime=5.0, code=0),
    ]
    sup, _ = _supervisor(tmp_path, children, clock)
    assert sup.run() == 0
    lines = _restart_lines(tmp_path)
    escalations = [ln for ln in lines if ln["exit_code"] == 3]
    assert len(escalations) == 2
    assert all(ln["reason"] == "watchdog-escalation" for ln in escalations)
    assert len([ln for ln in lines if ln["exit_code"] == 1]) == 1


def test_backoff_doubles_caps_and_resets(tmp_path: Path) -> None:
    clock = FakeClock()
    fast = lambda code=1: FakeChild(clock, runtime=1.0, code=code)  # noqa: E731
    children = [
        fast(),  # delay 5
        fast(),  # delay 10
        fast(),  # delay 20 (cap)
        fast(),  # delay 20 (capped)
        FakeChild(clock, runtime=120.0, code=1),  # stable run -> reset: 5
        fast(),  # first fast crash after the reset: still 5
        FakeChild(clock, runtime=1.0, code=0),  # clean exit ends it
    ]
    sup, _ = _supervisor(
        tmp_path,
        children,
        clock,
        backoff_base_s=5.0,
        backoff_cap_s=20.0,
        stable_after_s=60.0,
    )
    assert sup.run() == 0
    delays = [ln["delay_s"] for ln in _restart_lines(tmp_path)]
    assert delays == [5.0, 10.0, 20.0, 20.0, 5.0, 5.0, 0.0]


def test_exit_2_logs_loudly_but_still_restarts(tmp_path: Path, caplog) -> None:
    clock = FakeClock()
    children = [
        FakeChild(clock, runtime=1.0, code=2),
        FakeChild(clock, runtime=1.0, code=0),
    ]
    sup, spawned = _supervisor(tmp_path, children, clock)
    with caplog.at_level("ERROR"):
        assert sup.run() == 0
    assert len(spawned) == 2, "exit 2 must restart (owner ruling D6)"
    assert any("fix the config" in r.message for r in caplog.records)
    assert _restart_lines(tmp_path)[0]["reason"] == "config-error"


def test_stop_file_stops_gracefully(tmp_path: Path) -> None:
    clock = FakeClock()
    child = FakeChild(clock, runtime=math.inf, code=1)
    stop_file = tmp_path / "supervisor.stop"
    clock.on_sleep.append(
        lambda c: stop_file.touch() if c.now >= 3.0 else None
    )
    sup, _ = _supervisor(tmp_path, [child], clock)
    assert sup.run() == 0
    assert child.signals, "the child must be signalled to drain"
    assert not stop_file.exists(), "the stop-file is consumed"
    lines = _restart_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["reason"] == "stop-requested"


def test_stop_during_backoff_exits_promptly(tmp_path: Path) -> None:
    clock = FakeClock()
    children = [FakeChild(clock, runtime=1.0, code=1)]
    stop_file = tmp_path / "supervisor.stop"
    clock.on_sleep.append(
        lambda c: stop_file.touch() if c.now >= 2.0 else None
    )
    sup, spawned = _supervisor(
        tmp_path, children, clock, backoff_base_s=500.0
    )
    assert sup.run() == 0
    assert len(spawned) == 1, "no respawn after a stop during backoff"
    assert not stop_file.exists()


def test_stale_stop_file_at_startup_is_removed_and_ignored(
    tmp_path: Path,
) -> None:
    stop_file = tmp_path / "supervisor.stop"
    stop_file.touch()
    clock = FakeClock()
    sup, spawned = _supervisor(
        tmp_path, [FakeChild(clock, runtime=1.0, code=0)], clock
    )
    assert sup.run() == 0
    assert len(spawned) == 1, "a stale stop-file must not refuse startup"
    assert not stop_file.exists()


def test_unresponsive_child_is_killed_after_grace(tmp_path: Path) -> None:
    clock = FakeClock()
    child = FakeChild(clock, runtime=math.inf, code=1)
    child.send_signal = lambda sig: None  # type: ignore[method-assign] # ignores the drain request
    stop_file = tmp_path / "supervisor.stop"
    clock.on_sleep.append(
        lambda c: stop_file.touch() if c.now >= 1.0 else None
    )
    sup, _ = _supervisor(tmp_path, [child], clock, grace_s=15.0)
    assert sup.run() == 0
    assert child.kills == 1


def test_reason_mapping() -> None:
    assert _reason_for(0) == "clean-exit"
    assert _reason_for(1) == "software-failure"
    assert _reason_for(2) == "config-error"
    assert _reason_for(3) == "watchdog-escalation"
    assert _reason_for(4) == "lock-contention"
    assert _reason_for(-9) == "nonzero-exit"


# -- real children -------------------------------------------------------------


def test_real_children_exit_codes_recorded_faithfully(tmp_path: Path) -> None:
    """Real spawn path: a child that exits 3 is recorded as 3, repeatedly,
    until the operator's stop-file ends supervision."""
    opts = SupervisorOptions(
        data_dir=tmp_path,
        command=[sys.executable, "-c", "import sys; sys.exit(3)"],
        backoff_base_s=0.05,
        stable_after_s=0.0,  # every run counts as stable: constant delay
        poll_interval_s=0.05,
    )
    sup = Supervisor(opts)
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(sup.run()), daemon=True)
    t.start()
    restarts = tmp_path / "logs" / "restarts.jsonl"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if restarts.exists() and len(restarts.read_text().splitlines()) >= 2:
            break
        time.sleep(0.05)
    (tmp_path / "supervisor.stop").touch()
    t.join(timeout=30.0)
    assert not t.is_alive(), "supervisor failed to stop on the stop-file"
    assert result == [0]
    lines = _restart_lines(tmp_path)
    assert len(lines) >= 2
    assert all(ln["exit_code"] == 3 for ln in lines[:2])
    assert all(ln["reason"] == "watchdog-escalation" for ln in lines[:2])


def test_python_dash_m_palletscan_works() -> None:
    """D12: the supervisor/demo/CPU tools spawn ``python -m palletscan``."""
    import palletscan

    out = subprocess.run(
        [sys.executable, "-m", "palletscan", "version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == palletscan.__version__


def test_second_supervisor_on_same_data_dir_exits_4(
    tmp_path: Path, capsys
) -> None:
    with hold_instance_lock(tmp_path / "palletscan.supervisor.lock"):
        rc = main(["supervise", "--data-dir", str(tmp_path), "--", "run"])
    assert rc == 4
    assert "another instance holds" in capsys.readouterr().err


def test_supervise_rejects_non_writer_child(tmp_path: Path, capsys) -> None:
    rc = main(["supervise", "--data-dir", str(tmp_path), "--", "dashboard"])
    assert rc == 2
    assert "run, synth or replay" in capsys.readouterr().err
    rc = main(["supervise", "--data-dir", str(tmp_path)])
    assert rc == 2
