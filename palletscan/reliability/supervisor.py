"""Restart-on-any-nonzero-exit supervisor (Phase 5, D4–D7).

Task Scheduler alone cannot supervise a 24/7 station: ``-RestartInterval``
has a 1-minute floor, ``-RestartCount`` is bounded, and only the *last*
run result survives — per-restart exit codes are not countable. This
supervisor restarts the child in ~5 s, appends one JSONL line per child
exit to ``<data-dir>/logs/restarts.jsonl`` (``ts``, ``exit_code``,
``runtime_s``, ``delay_s``, ``reason``) so escalations (exit 3) vs crashes
(exit 1) are a one-liner to count, and stops cleanly via a stop-file
(``<data-dir>/supervisor.stop``) — console-ctrl events cannot cross
Windows sessions, so an operator's PowerShell writes a file instead.
Task Scheduler's only jobs: start the supervisor at logon and restart the
*supervisor* if it ever dies.

Policy (owner rulings):

- **Any** nonzero exit restarts, including 2 (usage/config error) — a
  station must come back by itself once ops fixes the file; exit 2 logs a
  loud "fix the config" line but still retries.
- Crash-loop backoff: base delay; a child that ran < ``stable_after_s``
  doubles it, capped; a stable run resets it. Clean exit 0 ends
  supervision (an intentional stop).
- Graceful stop: CTRL_BREAK on Windows (the child is spawned with
  ``CREATE_NEW_PROCESS_GROUP`` sharing the console — the only ctrl event
  deliverable to a child group), SIGTERM on POSIX; ``grace_s`` to drain,
  then kill.

Spawn/clock/sleep are injectable seams so the restart/backoff/stop logic
is unit-testable cross-platform without real processes.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from palletscan.logging_setup import RESTARTS_LOG_NAME

log = logging.getLogger(__name__)

#: Operator stop channel: write this file next to the data dir and the
#: supervisor stops the child gracefully, removes it, and exits 0.
STOP_FILE_NAME = "supervisor.stop"

_EXIT_REASONS = {
    0: "clean-exit",
    1: "software-failure",
    2: "config-error",
    3: "watchdog-escalation",
    4: "lock-contention",
}


def _reason_for(code: int) -> str:
    return _EXIT_REASONS.get(code, "nonzero-exit")


class ChildProcess(Protocol):
    """What the supervisor needs from a spawned child (Popen-compatible)."""

    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


def _default_spawn(command: list[str]) -> ChildProcess:
    """Spawn the child with inherited stdio (no pipes — a full pipe would
    deadlock a chatty child) and, on Windows, its own process group so
    CTRL_BREAK can target it without hitting the supervisor."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, **kwargs)


@dataclass(frozen=True, slots=True)
class SupervisorOptions:
    data_dir: Path
    command: list[str]
    grace_s: float = 15.0
    backoff_base_s: float = 5.0
    backoff_cap_s: float = 300.0
    stable_after_s: float = 60.0
    poll_interval_s: float = 0.5


class Supervisor:
    """Run ``opts.command`` forever, restarting on any nonzero exit.

    :meth:`run` blocks until the child exits 0, a stop is requested (the
    stop-file appears or :meth:`request_stop` is called), or the backoff
    sleep is interrupted by one of those. It returns the supervisor's own
    exit code (0 for every intentional path).
    """

    def __init__(
        self,
        opts: SupervisorOptions,
        *,
        spawn: Callable[[list[str]], ChildProcess] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.opts = opts
        self._spawn = spawn if spawn is not None else _default_spawn
        self._clock = clock
        self._sleep = sleep
        self._stop_event = threading.Event()
        # Whether the graceful stop should signal the child. False when the
        # child provably received the same terminal signal already (POSIX
        # Ctrl-C hits the whole foreground group): signalling it again
        # would trip its second-signal-forces handler mid-drain.
        self._forward = True

    @property
    def stop_file(self) -> Path:
        return self.opts.data_dir / STOP_FILE_NAME

    @property
    def restarts_path(self) -> Path:
        return self.opts.data_dir / "logs" / RESTARTS_LOG_NAME

    def request_stop(self, forward: bool = True) -> None:
        """Stop the child and exit 0 (signal-handler safe)."""
        self._forward = forward
        self._stop_event.set()

    # -- internals -----------------------------------------------------------

    def _stop_requested(self) -> bool:
        return self._stop_event.is_set() or self.stop_file.exists()

    def _append_restart(
        self, exit_code: int, runtime_s: float, delay_s: float, reason: str
    ) -> None:
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "exit_code": exit_code,
            "runtime_s": round(runtime_s, 3),
            "delay_s": delay_s,
            "reason": reason,
        }
        path = self.restarts_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    def stop_child_gracefully(self, child: ChildProcess) -> int:
        """Signal (unless the child already got one), drain, then kill."""
        if self._forward:
            try:
                if sys.platform == "win32":
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    child.send_signal(signal.SIGTERM)
            except (OSError, ValueError):
                pass  # already gone
        try:
            return child.wait(timeout=self.opts.grace_s)
        except subprocess.TimeoutExpired:
            log.warning(
                "child pid %d did not drain within %.0fs grace; killing",
                child.pid,
                self.opts.grace_s,
            )
            child.kill()
            return child.wait()

    def _wait_for_exit_or_stop(self, child: ChildProcess) -> tuple[int, bool]:
        """Returns (exit_code, stop_requested)."""
        while True:
            code = child.poll()
            if code is not None:
                return code, False
            if self._stop_requested():
                return self.stop_child_gracefully(child), True
            self._sleep(self.opts.poll_interval_s)

    def _backoff_sleep(self, delay_s: float) -> bool:
        """Sleep ``delay_s``, polling for stop. True if stop interrupted."""
        end = self._clock() + delay_s
        while self._clock() < end:
            if self._stop_requested():
                return True
            self._sleep(min(self.opts.poll_interval_s, end - self._clock()))
        return False

    # -- entry point -----------------------------------------------------------

    def run(self) -> int:
        if self.stop_file.exists():
            # Leftover from a hard stop: refusing to start over a stale
            # marker would strand the station; remove and carry on.
            self.stop_file.unlink(missing_ok=True)
            log.info("removed stale stop-file %s", self.stop_file)
        delay = self.opts.backoff_base_s
        while True:
            started = self._clock()
            try:
                child = self._spawn(self.opts.command)
            except OSError as exc:
                log.error("could not spawn %s: %s", self.opts.command, exc)
                return 1
            log.info("child pid %d spawned: %s", child.pid, self.opts.command)
            code, stopped = self._wait_for_exit_or_stop(child)
            runtime = self._clock() - started
            if stopped:
                self._append_restart(code, runtime, 0.0, "stop-requested")
                self.stop_file.unlink(missing_ok=True)
                log.info(
                    "stop requested; child exited %d after %.1fs", code, runtime
                )
                return 0
            if code == 0:
                self._append_restart(0, runtime, 0.0, "clean-exit")
                log.info("child exited 0 after %.1fs; supervision ends", runtime)
                return 0
            if runtime >= self.opts.stable_after_s:
                delay = self.opts.backoff_base_s  # stable run resets backoff
            self._append_restart(code, runtime, delay, _reason_for(code))
            if code == 2:
                log.error(
                    "child exited 2 (usage/config error): fix the config; "
                    "the supervisor will pick it up on the next retry"
                )
            log.warning(
                "child exited %d (%s) after %.1fs; restarting in %.0fs",
                code,
                _reason_for(code),
                runtime,
                delay,
            )
            if self._backoff_sleep(delay):
                self.stop_file.unlink(missing_ok=True)
                log.info("stop requested during backoff; supervision ends")
                return 0
            if runtime < self.opts.stable_after_s:
                delay = min(delay * 2, self.opts.backoff_cap_s)
