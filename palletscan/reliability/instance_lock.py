"""Single-instance lock, scoped per data-dir (Phase 5, D1/D2).

OS-level locking, not a PID file: the OS releases the lock when the holder
dies — cleanly or not — so **stale locks are structurally impossible** (no
liveness probing, no PID-reuse races).

Mechanics:

- POSIX: ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the whole file. flock is
  per open-file-description, so a second ``open()`` + ``flock`` fails even
  inside the holding process.
- Windows: ``msvcrt.locking(LK_NBLCK)`` on one byte at
  ``LOCK_BYTE_OFFSET`` (1 MiB; locking past EOF is legal). Windows
  byte-range locks are *mandatory* — a locked byte is unreadable by any
  other handle — so the lock byte sits far from offset 0, keeping the
  holder-diagnostics JSON written there readable for ops.
- ``release()`` unlocks before closing (MSDN: release timing after an
  abnormal close is indeterminate; the supervisor's >= 5 s restart delay
  covers that window).
- The lock file is **never unlinked**: delete-while-open races a fresh
  acquirer, and the leftover file is harmless last-holder diagnostics.

Lock handles never leak into child processes (PEP 446: fds/handles are
non-inheritable by default).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Where the lock byte lives (see module docstring). Past EOF by design.
LOCK_BYTE_OFFSET = 0x100000

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def read_holder(path: Path | str) -> dict:
    """Best-effort read of the holder diagnostics JSON (file offset 0).

    Returns ``{}`` when the file is missing/unreadable/garbled — the
    diagnostics are for error messages and ops, never for correctness.
    """
    try:
        with open(path, "rb") as fh:
            line = fh.readline(4096)
        info = json.loads(line.decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


class InstanceLockHeld(Exception):
    """Another process holds the lock (CLI exit code 4)."""

    def __init__(self, path: Path, holder: dict) -> None:
        self.path = path
        self.holder = holder
        if holder:
            who = (
                f"pid {holder.get('pid')}, started {holder.get('started')}, "
                f"argv {holder.get('argv')}"
            )
        else:
            who = "holder diagnostics unreadable"
        super().__init__(
            f"another instance holds {path} ({who}); stop it first, or use "
            "a different --data-dir to run side by side"
        )


class InstanceLock:
    """One OS-level lock on ``path``, held for the process lifetime.

    ``acquire`` raises :class:`InstanceLockHeld` on contention; ``release``
    is idempotent. Holder diagnostics (pid, start time, argv) are written
    at offset 0 by the winner only.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return  # this object already holds it
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            os.close(fd)
            raise InstanceLockHeld(self.path, read_holder(self.path)) from None
        self._fd = fd
        self._write_holder()

    def _write_holder(self) -> None:
        assert self._fd is not None
        info = {
            "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "argv": sys.argv,
        }
        data = (json.dumps(info) + "\n").encode("utf-8")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data)
        os.ftruncate(self._fd, len(data))

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        except OSError:
            pass  # the close below releases it anyway
        finally:
            os.close(fd)
        # Never unlink: the file doubles as last-holder diagnostics, and
        # delete-while-open races a concurrent fresh acquirer.


@contextmanager
def hold_instance_lock(path: Path | str) -> Iterator[InstanceLock]:
    """Acquire for the ``with`` body; release on exit (never unlinks)."""
    lock = InstanceLock(path)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
