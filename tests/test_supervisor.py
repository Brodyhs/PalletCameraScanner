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
    # Sticky latch (REVIEW findings 13/15): the supervisor never deletes
    # the stop-file — a Task Scheduler revival must keep honoring it.
    assert stop_file.exists(), "the stop-file is a latch, never consumed"
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
    assert stop_file.exists(), "the stop-file is a latch, never consumed"


def test_stop_file_at_startup_is_honored_not_discarded(
    tmp_path: Path,
) -> None:
    """REVIEW finding 15 (repro-derived): a stop request written while the
    supervisor was down (Task Scheduler restart window, reboot) used to be
    unlinked as 'stale' and the child spawned — the operator's stop script
    saw the file vanish and printed success while the station started up.
    A starting supervisor must HONOR the request: no spawn, exit 0, audit
    line, and the latch stays for the next revival."""
    stop_file = tmp_path / "supervisor.stop"
    stop_file.touch()
    clock = FakeClock()
    sup, spawned = _supervisor(
        tmp_path, [FakeChild(clock, runtime=1.0, code=0)], clock
    )
    assert sup.run() == 0
    assert spawned == [], "a live stop request must never start the child"
    assert stop_file.exists(), "the latch survives for the next revival"
    lines = _restart_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["reason"] == "stop-honored-at-startup"
    assert lines[0]["exit_code"] is None


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


# -- REVIEW_SYSTEM_0c30c77 findings 13 and 6 -----------------------------------


def test_unwritable_audit_log_does_not_kill_the_restart(tmp_path: Path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 13 (repro: child crashes at 02:00,
    the supervisor's restarts.jsonl append raises ENOSPC, the supervisor
    dies instead of restarting in 5 s — the station goes fully dark while
    the disk stays full). The append is bookkeeping; supervision outranks
    it."""
    (tmp_path / "logs").write_text("not a directory")  # mkdir will raise
    clock = FakeClock()
    children = [
        FakeChild(clock, runtime=10.0, code=1),
        FakeChild(clock, runtime=10.0, code=0),
    ]
    sup, spawned = _supervisor(tmp_path, children, clock)
    assert sup.run() == 0
    assert len(spawned) == 2, "the crash must still be restarted"


def test_unwritable_audit_log_does_not_derail_the_stop(tmp_path: Path) -> None:
    """Finding 13, stop variant: the same ENOSPC fired on the stop path
    before the stop handling completed."""
    (tmp_path / "logs").write_text("not a directory")
    clock = FakeClock()
    child = FakeChild(clock, runtime=math.inf, code=1)
    stop_file = tmp_path / "supervisor.stop"
    clock.on_sleep.append(
        lambda c: stop_file.touch() if c.now >= 2.0 else None
    )
    sup, _ = _supervisor(tmp_path, [child], clock)
    assert sup.run() == 0
    assert child.signals, "the child must still be stopped gracefully"


def test_parent_watch_fires_once_when_parent_dies() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 6: the writer child's half of the
    orphan protection — a dead supervisor must make the child self-stop so
    a replacement supervisor can win the lock instead of churning exit-4
    against an unstoppable orphan."""
    from palletscan.reliability.supervisor import ParentWatch

    alive = {"value": True}
    fired: list[int] = []
    watch = ParentWatch(
        4242, lambda: fired.append(1), poll_s=0.01, alive=lambda: alive["value"]
    )
    watch.start()
    time.sleep(0.05)
    assert fired == [], "must not fire while the parent lives"
    alive["value"] = False
    deadline = time.monotonic() + 2.0
    while not fired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fired == [1], "on_dead fires exactly once"
    watch.stop()


def test_parent_watch_stop_ends_the_thread_without_firing() -> None:
    """Finding 6 + the in-process-main no-leak convention: the watch must
    terminate when the writer command finishes."""
    from palletscan.reliability.supervisor import ParentWatch

    fired: list[int] = []
    watch = ParentWatch(
        4242, lambda: fired.append(1), poll_s=0.01, alive=lambda: True
    )
    watch.start()
    watch.stop()
    assert fired == []
    assert not watch._thread.is_alive()


def test_parent_watch_treats_alive_check_error_as_dead() -> None:
    from palletscan.reliability.supervisor import ParentWatch

    def broken() -> bool:
        raise OSError("cannot probe")

    fired: list[int] = []
    watch = ParentWatch(4242, lambda: fired.append(1), poll_s=0.01, alive=broken)
    watch.start()
    deadline = time.monotonic() + 2.0
    while not fired and time.monotonic() < deadline:
        time.sleep(0.01)
    watch.stop()
    assert fired == [1]


def test_default_spawn_injects_pid_env_without_mutating_environ(
    monkeypatch,
) -> None:
    """Finding 6 + design-review fix: the env var must ride the Popen env
    argument only — a mutated os.environ leaks into in-process pytest
    main() calls and into writer children spawned by other tools, handing
    them a bogus supervisor pid that makes them self-stop seconds in."""
    import os

    from palletscan.reliability.supervisor import (
        SUPERVISOR_PID_ENV,
        _default_spawn,
    )

    recorded: dict = {}

    class _FakeProc:
        pid = 1

    def fake_popen(command, **kwargs):
        recorded["command"] = command
        recorded.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _default_spawn(["x"])
    assert recorded["env"][SUPERVISOR_PID_ENV] == str(os.getpid())
    assert SUPERVISOR_PID_ENV not in os.environ


def test_supervisor_crash_stops_live_child_before_propagating(
    tmp_path: Path,
) -> None:
    """Finding 6, supervisor half: an unexpected supervisor exception must
    not strand an unstoppable orphan that holds the instance lock and
    keeps scanning under a stale config."""
    clock = FakeClock()
    child = FakeChild(clock, runtime=math.inf, code=1)
    sup, _ = _supervisor(tmp_path, [child], clock)
    calls = {"n": 0}
    original_sleep = clock.sleep

    def exploding_sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("supervisor bug")
        original_sleep(seconds)

    sup._sleep = exploding_sleep
    with pytest.raises(RuntimeError, match="supervisor bug"):
        sup.run()
    assert child.signals, "the child must be stopped before the crash escapes"
    assert child.poll() is not None, "the child must be dead"


# -- StopFileWatch: the writer-level stop latch --------------------------------


def test_stop_file_watch_fires_on_appearance(tmp_path: Path) -> None:
    """The console-free stop channel: touching the latch drains the writer
    (CTRL_BREAK needs a shared console, which CI capture often lacks)."""
    from palletscan.reliability.supervisor import StopFileWatch

    stopped = threading.Event()
    latch = tmp_path / "palletscan.stop"
    watch = StopFileWatch(latch, stopped.set, poll_s=0.02)
    watch.start()
    try:
        assert not stopped.wait(0.1), "must not fire before the file exists"
        latch.touch()
        assert stopped.wait(2.0), "must fire once the latch appears"
        assert latch.exists(), "the latch is sticky: the watch never deletes it"
    finally:
        watch.stop()


def test_stop_file_watch_pre_existing_latch_fires_immediately(
    tmp_path: Path,
) -> None:
    """Startup honoring, matching the supervisor latch semantics."""
    from palletscan.reliability.supervisor import StopFileWatch

    stopped = threading.Event()
    latch = tmp_path / "palletscan.stop"
    latch.touch()
    watch = StopFileWatch(latch, stopped.set, poll_s=0.02)
    watch.start()
    try:
        assert stopped.wait(2.0), "a latch present at start must fire"
    finally:
        watch.stop()


def test_stop_file_watch_stop_joins_without_firing(tmp_path: Path) -> None:
    """The _WriterLease convention: no leaked threads, no spurious drain."""
    from palletscan.reliability.supervisor import StopFileWatch

    fired = threading.Event()
    watch = StopFileWatch(tmp_path / "absent.stop", fired.set, poll_s=0.02)
    watch.start()
    watch.stop()
    assert not watch._thread.is_alive(), "stop() must join the thread"
    assert not fired.is_set()
