"""RollingFrameBuffer: time-bounded pre/post evidence buffer.

Eviction keys off the frame source clock (``Frame.ts``), so accelerated and
non-realtime runs behave exactly like realtime. ``maxlen`` is a hard memory
safety net on top of the time horizon.
"""

from __future__ import annotations

from collections import deque

from palletscan.types import Frame


class RollingFrameBuffer:
    """Keeps the last ``horizon_s`` seconds of frames (source clock)."""

    def __init__(self, horizon_s: float, maxlen: int = 512) -> None:
        self._horizon_s = horizon_s
        self._frames: deque[Frame] = deque(maxlen=maxlen)

    def append(self, frame: Frame) -> None:
        self._frames.append(frame)
        cutoff = frame.ts - self._horizon_s
        while self._frames and self._frames[0].ts < cutoff:
            self._frames.popleft()

    def extract(self, t0: float, t1: float) -> list[Frame]:
        """Frames with ``t0 <= ts <= t1``, oldest first."""
        return [f for f in self._frames if t0 <= f.ts <= t1]

    def __len__(self) -> int:
        return len(self._frames)
