"""Restart-on-any-nonzero-exit supervisor (Phase 5, D4–D7).

Task Scheduler alone cannot supervise a 24/7 station: ``-RestartInterval``
has a 1-minute floor, ``-RestartCount`` is bounded, and only the *last*
run result survives — per-restart exit codes are not countable. This
supervisor restarts the child in ~5 s, appends one JSONL line per child
exit to ``<data-dir>/logs/restarts.jsonl`` (``ts``, ``exit_code``,
``runtime_s``, ``delay_s``, ``reason``) so escalations (exit 3) vs crashes
(exit 1) are a one-liner to count, and stops cleanly via a stop-file
(``<data-dir>/supervisor.stop``) — console-ctrl events cannot cross
Windows sessions, so an operator's PowerShell writes a file instead. The
stop-file is a sticky latch: the supervisor never deletes it, and one
present at startup is honored (exit 0, no spawn, audit line) — a stop
request survives Task Scheduler revivals and reboots until an explicit
start removes the file (REVIEW findings 13/15). Task Scheduler's only
jobs: start the supervisor at logon and restart the *supervisor* if it
ever dies.

Child lifetime is tied to supervisor lifetime (REVIEW finding 6): on
Windows the child joins a kill-on-close job object whose only handle the
supervisor holds; everywhere, the child gets the supervisor's pid in
``SUPERVISOR_PID_ENV`` and self-stops (graceful drain) when that process
dies; and an unexpected supervisor exception stops the child before
propagating. "Stopped" is verified, never assumed: the stop tooling
probes the instance locks (whose OS-level release is death-proof) instead
of trusting stop-file consumption.

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
import os
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

#: Operator stop channel: write this file in the data dir. The supervisor
#: stops the child gracefully and exits 0 — and the file is a STICKY LATCH:
#: the supervisor never removes it, and a supervisor starting while it
#: exists honors it (exits 0 without spawning). A stop request can
#: therefore never be discarded by a Task Scheduler revival or a reboot
#: (REVIEW findings 13/15); only an explicit start (start_palletscan.ps1,
#: or deleting the file) re-arms the station.
STOP_FILE_NAME = "supervisor.stop"

#: Env var carrying the supervisor's pid to its writer child. The child
#: watches that process and self-stops (graceful drain) when it dies, so a
#: dead supervisor can never strand an unstoppable orphan that holds the
#: instance lock and keeps scanning under a stale config (REVIEW finding
#: 6). Injected per spawn via the Popen env argument — never by mutating
#: os.environ, which would leak into in-process pytest main() calls and
#: into writer children spawned by other tools.
SUPERVISOR_PID_ENV = "PALLETSCAN_SUPERVISOR_PID"

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


if sys.platform == "win32":

    def _assign_kill_on_close_job(proc: subprocess.Popen) -> object | None:
        """Tie the child's lifetime to this process via a Job Object.

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE with the only job handle held
        here (attached to the Popen object): ANY supervisor death — crash,
        taskkill, Stop-ScheduledTask — closes the handle and the OS
        terminates the child, so the orphaned-writer state of REVIEW
        finding 6 cannot arise. Best-effort: on assignment failure the
        child-side parent watch (SUPERVISOR_PID_ENV) is the backstop.
        Windows-only by nature; executed verification is an
        ARRIVAL_CHECKLIST §9 item.
        """
        import ctypes
        from ctypes import wintypes

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),  # ULONG_PTR
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        _JobObjectExtendedLimitInformation = 9
        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            log.warning("CreateJobObject failed (%d); relying on the "
                        "child-side parent watch", k32.GetLastError())
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        # Popen._handle is a private CPython attribute; a Popen-shaped object
        # without one (test fakes, alternative runtimes) gets the same
        # degraded path as any other job-object failure below.
        handle = getattr(proc, "_handle", None)
        if handle is None:
            log.warning(
                "child has no OS process handle; relying on the "
                "child-side parent watch"
            )
            k32.CloseHandle(job)
            return None
        ok = k32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ) and k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(handle)))
        if not ok:
            log.warning(
                "job-object assignment failed (%d); relying on the "
                "child-side parent watch", k32.GetLastError(),
            )
            k32.CloseHandle(job)
            return None
        return job

    def _make_parent_alive_check(pid: int) -> Callable[[], bool]:
        """Liveness check that survives pid reuse: open a SYNCHRONIZE
        handle ONCE while the parent is known alive and poll it — the
        retained handle pins the process object, so a recycled pid can
        never fake liveness (a false-alive here means an orphan that never
        stops)."""
        import ctypes

        _SYNCHRONIZE = 0x00100000
        _WAIT_TIMEOUT = 0x102
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return lambda: False  # already gone (or unopenable: fail safe)

        def alive() -> bool:
            return bool(k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)

        return alive

else:

    def _assign_kill_on_close_job(proc: subprocess.Popen) -> object | None:
        return None  # POSIX: the parent watch + crash-path stop cover it

    def _make_parent_alive_check(pid: int) -> Callable[[], bool]:
        """The writer is a direct child of the supervisor: orphaning
        reparents it (POSIX), so a changed ppid is a pid-reuse-proof death
        signal."""
        return lambda: os.getppid() == pid


class ParentWatch:
    """Daemon thread that fires ``on_dead`` once when the watched process
    dies (the writer child's half of the finding-6 orphan protection).

    ``stop()`` must run when the writer command finishes: main() runs
    in-process under pytest and process-global acquisitions must not leak
    across calls (the ``_WriterLease`` convention) — a poll-forever thread
    holding a finished runner would. ``alive`` is injectable for tests.
    """

    def __init__(
        self,
        pid: int,
        on_dead: Callable[[], None],
        *,
        poll_s: float = 2.0,
        alive: Callable[[], bool] | None = None,
    ) -> None:
        self._pid = pid
        self._on_dead = on_dead
        self._poll_s = poll_s
        self._alive = alive if alive is not None else _make_parent_alive_check(pid)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="parent-watch", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                ok = self._alive()
            except Exception:  # pragma: no cover - defensive
                ok = False
            if not ok:
                log.error(
                    "supervisor (pid %d) is gone; stopping this writer so a "
                    "replacement supervisor can own the station (orphan "
                    "protection, REVIEW finding 6)",
                    self._pid,
                )
                try:
                    self._on_dead()
                finally:
                    return


class StopFileWatch:
    """Daemon thread that fires ``on_stop`` once when the watched stop-file
    appears — the writer-level half of the stop-latch channel.

    The supervisor watches ``supervisor.stop``; an unsupervised writer
    (``run``/``synth``/``replay``) watches ``palletscan.stop`` next to its
    instance lock, so scripts (and tools/demo.py's smoke stop) can request
    a graceful drain without a console: CTRL_BREAK needs a shared console,
    which services and captured-output CI shells often lack. Like the
    supervisor's, the latch is sticky — this class never deletes the file;
    whoever created it clears it.

    ``stop()`` must run when the writer command finishes (the
    ``_WriterLease`` convention: main() runs in-process under pytest and
    must not leak threads). A file already present at start fires
    immediately, matching the supervisor's startup-latch honoring.
    """

    def __init__(
        self,
        path: Path,
        on_stop: Callable[[], None],
        *,
        poll_s: float = 0.25,
    ) -> None:
        self._path = path
        self._on_stop = on_stop
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="stop-file-watch", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while True:
            try:
                present = self._path.exists()
            except OSError:  # pragma: no cover - defensive
                present = False
            if present:
                log.info(
                    "stop-file %s present: draining this writer", self._path
                )
                try:
                    self._on_stop()
                finally:
                    return
            if self._stop.wait(self._poll_s):
                return


def _default_spawn(command: list[str]) -> ChildProcess:
    """Spawn the child with inherited stdio (no pipes — a full pipe would
    deadlock a chatty child) and, on Windows, its own process group so
    CTRL_BREAK can target it without hitting the supervisor. The child gets
    this process's pid in SUPERVISOR_PID_ENV (parent watch) and, on
    Windows, joins a kill-on-close job object — both halves of tying the
    child's lifetime to the supervisor's (REVIEW finding 6)."""
    kwargs: dict = {
        "env": {**os.environ, SUPERVISOR_PID_ENV: str(os.getpid())},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(command, **kwargs)
    job = _assign_kill_on_close_job(proc)
    if job is not None:
        # The job handle must live exactly as long as the supervisor holds
        # the child; parking it on the Popen object does that.
        proc._palletscan_job = job  # type: ignore[attr-defined]
    return proc


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
        self,
        exit_code: int | None,
        runtime_s: float,
        delay_s: float,
        reason: str,
    ) -> None:
        """Best-effort audit line. NEVER raises: an unwritable audit log
        (full disk — the same condition that crashes the child) must not
        kill the supervisor that exists to restart it, and on the stop path
        it must not derail the stop handling (REVIEW finding 13)."""
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "exit_code": exit_code,
            "runtime_s": round(runtime_s, 3),
            "delay_s": delay_s,
            "reason": reason,
        }
        path = self.restarts_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError as exc:
            log.error(
                "could not append to %s (%r); continuing — supervision "
                "outranks its own bookkeeping",
                path,
                exc,
            )

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
            # The stop-file is a sticky latch, never discarded (REVIEW
            # findings 13/15): a stop written while no supervisor was
            # looking — during a Task Scheduler restart window, before a
            # reboot — must keep the station stopped, not be deleted as
            # "stale" while the operator's stop script reports success.
            # Only an explicit start (start_palletscan.ps1 removes the
            # file) re-arms the station.
            self._append_restart(None, 0.0, 0.0, "stop-honored-at-startup")
            log.warning(
                "stop-file %s present at startup: honoring the stop "
                "request and not starting the child (remove the file or "
                "use start_palletscan.ps1 to start the station)",
                self.stop_file,
            )
            return 0
        delay = self.opts.backoff_base_s
        child: ChildProcess | None = None
        try:
            while True:
                started = self._clock()
                try:
                    child = self._spawn(self.opts.command)
                except OSError as exc:
                    log.error("could not spawn %s: %s", self.opts.command, exc)
                    return 1
                log.info(
                    "child pid %d spawned: %s", child.pid, self.opts.command
                )
                code, stopped = self._wait_for_exit_or_stop(child)
                runtime = self._clock() - started
                if stopped:
                    # The child is verified dead here: _wait_for_exit_or_stop
                    # returns only after wait() (grace, then kill+wait). The
                    # stop-file, if any, stays — see the latch note above.
                    self._append_restart(code, runtime, 0.0, "stop-requested")
                    log.info(
                        "stop requested; child exited %d after %.1fs",
                        code,
                        runtime,
                    )
                    return 0
                if code == 0:
                    self._append_restart(0, runtime, 0.0, "clean-exit")
                    log.info(
                        "child exited 0 after %.1fs; supervision ends", runtime
                    )
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
                    log.info("stop requested during backoff; supervision ends")
                    return 0
                if runtime < self.opts.stable_after_s:
                    delay = min(delay * 2, self.opts.backoff_cap_s)
        except BaseException:
            # A supervisor bug must not strand an unstoppable orphan that
            # holds the instance lock and keeps scanning under a stale
            # config (REVIEW finding 6): take the child down (gracefully —
            # signal, grace, kill) before propagating.
            if child is not None and child.poll() is None:
                log.error(
                    "supervisor failed with child pid %d alive; stopping "
                    "the child before exiting",
                    child.pid,
                )
                self.stop_child_gracefully(child)
            raise
