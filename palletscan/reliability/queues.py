"""Bounded queues with explicit backpressure policy."""

from __future__ import annotations

import queue
from typing import Any

#: End-of-stream marker passed through queues (identity-compared).
SENTINEL: object = object()


class DroppingQueue:
    """Bounded queue that drops the *oldest* item when full.

    Used for frames: under decode pressure, losing the oldest frame of a
    pass is harmless (a pass spans dozens of frames) while blocking the
    capture thread would not be. Drops are counted, never silent.

    Single-producer / single-consumer; the drop counter is maintained by
    the producer thread only.
    """

    def __init__(self, maxsize: int) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, item: Any) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self.dropped += 1
                except queue.Empty:  # consumer drained it meanwhile
                    pass

    def get(self, timeout: float | None = None) -> Any:
        return self._q.get(timeout=timeout)

    def qsize(self) -> int:
        return self._q.qsize()
