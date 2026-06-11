"""FlakySource: failure injection for soak and recovery testing.

Wraps any :class:`FrameSource` and injects a stall (sleep) or a raised
exception at configured frame counts, so tests can prove the crash-only
contract: an injected failure must end the run through the normal flush
path (pending misses become events) with zero event loss, and a supervisor
restart resumes the load. The in-process watchdog that turns stalls into
reopens is Phase 3; ``stall_at`` exists so that work has a ready-made
fault to detect.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from palletscan.sources.base import FrameSource
from palletscan.types import Frame


class InjectedFailure(RuntimeError):
    """Raised by FlakySource at the configured frame (distinguishable from
    real failures in test assertions)."""


class FlakySource(FrameSource):
    """Delegates to ``inner``, failing on cue."""

    def __init__(
        self,
        inner: FrameSource,
        *,
        raise_at: int | None = None,
        stall_at: int | None = None,
        stall_s: float = 0.0,
    ) -> None:
        self.inner = inner
        self._raise_at = raise_at
        self._stall_at = stall_at
        self._stall_s = stall_s
        self.frames_emitted = 0
        #: Wall clock at the moment the injected failure was raised — the
        #: "outage began" edge for restart-gap measurement (run() teardown
        #: time after this instant is part of the outage).
        self.failed_wall: float | None = None

    @property
    def source_id(self) -> str:
        return self.inner.source_id

    @property
    def nominal_fps(self) -> float | None:
        return self.inner.nominal_fps

    @property
    def live(self) -> bool:
        return self.inner.live

    def frames(self) -> Iterator[Frame]:
        for frame in self.inner.frames():
            if self._raise_at is not None and self.frames_emitted >= self._raise_at:
                self.failed_wall = time.monotonic()
                raise InjectedFailure(
                    f"injected failure after {self.frames_emitted} frames"
                )
            if self._stall_at is not None and self.frames_emitted == self._stall_at:
                time.sleep(self._stall_s)
            yield frame
            self.frames_emitted += 1

    def close(self) -> None:
        self.inner.close()
