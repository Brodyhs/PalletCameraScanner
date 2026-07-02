"""Single-instance lock semantics (Phase 5, D1/D2) + CLI exit code 4."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from palletscan.cli import main
from palletscan.reliability.instance_lock import (
    InstanceLock,
    InstanceLockHeld,
    hold_instance_lock,
    read_holder,
)

_SYNTH_YAML = """
synthetic:
  width: 640
  height: 360
  num_passes: 1
  seed: 1234
  speed_mph_range: [3.0, 5.0]
  angle_deg_range: [0.0, 10.0]
  contrast_range: [0.8, 1.0]
  noise_sigma_range: [1.0, 3.0]
  occlusion_max_frac: 0.0
  idle_s_range: [0.4, 0.6]
sinks:
  console: {enabled: false}
"""

_HOLD_CHILD = """
import os, sys, time
from palletscan.reliability.instance_lock import InstanceLock

lock = InstanceLock(sys.argv[1])
lock.acquire()
print(f"held {os.getpid()}", flush=True)
time.sleep(120)
"""


def test_acquire_release_reacquire(tmp_path: Path) -> None:
    path = tmp_path / "palletscan.lock"
    lock = InstanceLock(path)
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held
    lock.release()  # idempotent
    lock.acquire()  # re-acquire after release
    lock.release()
    assert path.exists(), "the lock file is never unlinked (diagnostics)"


def test_second_acquire_in_same_process_fails(tmp_path: Path) -> None:
    path = tmp_path / "palletscan.lock"
    with hold_instance_lock(path):
        with pytest.raises(InstanceLockHeld) as exc_info:
            InstanceLock(path).acquire()
        assert str(os.getpid()) in str(exc_info.value)
    # released by the context manager -> a fresh acquire succeeds
    with hold_instance_lock(path):
        pass


def test_holder_json_readable_while_locked(tmp_path: Path) -> None:
    path = tmp_path / "palletscan.lock"
    with hold_instance_lock(path):
        holder = read_holder(path)
        assert holder["pid"] == os.getpid()
        assert "started" in holder and "argv" in holder
        # raw read of offset 0 works too (the lock byte is at 1 MiB)
        first_line = path.read_bytes().splitlines()[0]
        assert json.loads(first_line)["pid"] == os.getpid()


def test_hard_killed_holder_leaves_no_stale_lock(tmp_path: Path) -> None:
    """The D1 stale-lock proof: SIGKILL the holder (no cleanup runs) and
    the next acquire succeeds because the OS released the lock.

    The holder child reports ITS OWN pid: on a Windows venv, sys.executable
    is a launcher shim, so Popen.pid is the shim while the lock (correctly)
    records the real interpreter — the product contract is "the message
    names the actual holder", and the kill must target the actual holder
    too or the shim dies while the interpreter keeps the lock (the
    pre-existing Windows failure of this test)."""
    path = tmp_path / "palletscan.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_CHILD, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    holder_pid = proc.pid
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().split()
        assert line and line[0] == "held"
        holder_pid = int(line[1])  # the real interpreter, not the shim
        with pytest.raises(InstanceLockHeld) as exc_info:
            InstanceLock(path).acquire()
        assert str(holder_pid) in str(exc_info.value), "message names the holder"
    finally:
        if holder_pid != proc.pid:
            # Kill the real holder first (the shim's death won't release
            # the lock its child interpreter holds).
            subprocess.run(
                ["taskkill", "/PID", str(holder_pid), "/F"],
                capture_output=True,
            )
        proc.kill()
        proc.wait(timeout=30)
    # The kernel releases the lock with the process; allow a brief grace
    # for fd teardown, then the parent must win it.
    lock = InstanceLock(path)
    deadline = time.monotonic() + 10.0
    while True:
        try:
            lock.acquire()
            break
        except InstanceLockHeld:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    lock.release()


def test_run_exits_4_while_lock_held(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "synth.yaml"
    cfg.write_text(_SYNTH_YAML, encoding="utf-8")
    data_dir = tmp_path / "data"
    with hold_instance_lock(data_dir / "palletscan.lock"):
        rc = main(
            ["run", "--config", str(cfg), "--data-dir", str(data_dir)]
        )
    err = capsys.readouterr().err
    assert rc == 4
    assert "another instance holds" in err
    assert str(os.getpid()) in err, "contention message names the holder"


@pytest.mark.parametrize("command", ["synth", "replay"])
def test_other_writer_commands_exit_4_while_lock_held(
    tmp_path: Path, capsys, command: str
) -> None:
    cfg = tmp_path / "synth.yaml"
    cfg.write_text(_SYNTH_YAML, encoding="utf-8")
    data_dir = tmp_path / "data"
    argv = [command, "--config", str(cfg), "--data-dir", str(data_dir)]
    if command == "replay":
        # The clip never gets opened: the lock check comes first.
        argv.insert(1, str(tmp_path / "missing.avi"))
    with hold_instance_lock(data_dir / "palletscan.lock"):
        rc = main(argv)
    assert rc == 4
    assert "another instance holds" in capsys.readouterr().err


def test_writer_command_writes_rotating_log(tmp_path: Path, capsys) -> None:
    """D3 wiring: a writer command run produces data/logs/palletscan.jsonl
    with parseable JSON lines, and releases the handler afterwards."""
    import logging
    import logging.handlers

    cfg = tmp_path / "synth.yaml"
    cfg.write_text(_SYNTH_YAML, encoding="utf-8")
    data_dir = tmp_path / "data"
    rc = main(["run", "--config", str(cfg), "--data-dir", str(data_dir)])
    assert rc == 0, capsys.readouterr().out
    log_file = data_dir / "logs" / "palletscan.jsonl"
    assert log_file.is_file()
    for line in log_file.read_text(encoding="utf-8").splitlines():
        assert "ts" in json.loads(line)
    leftovers = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert leftovers == [], "the file handler must not leak across main() calls"


def test_dashboard_command_takes_no_lock(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """D2: dashboard is read-only and must coexist with a held lock."""
    import palletscan.cli as cli_mod

    cfg = tmp_path / "synth.yaml"
    cfg.write_text(_SYNTH_YAML + "web:\n  port: 0\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    rc = main(["run", "--config", str(cfg), "--data-dir", str(data_dir)])
    assert rc == 0, capsys.readouterr().out
    monkeypatch.setattr(cli_mod, "_wait_for_interrupt", lambda: None)
    with hold_instance_lock(data_dir / "palletscan.lock"):
        rc = main(
            ["dashboard", "--config", str(cfg), "--data-dir", str(data_dir)]
        )
    assert rc == 0, capsys.readouterr().err
