"""Bounded queues with explicit backpressure policy."""

from __future__ import annotations

import queue
from collections.abc import Callable
from typing import Any

#: End-of-stream marker passed through queues (identity-compared).
SENTINEL: object = object()


class DroppingQueue:
    """Bounded queue with two producer policies.

    :meth:`put` drops the *oldest* item when full — for live capture, where
    losing the oldest frame of a pass is harmless (a pass spans dozens of
    frames) while blocking the capture thread would not be. Drops are
    counted, never silent.

    :meth:`put_blocking` blocks instead — for finite replay sources, whose
    frames are all available; dropping them would fabricate data loss.

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

    def put_blocking(
        self, item: Any, abort: Callable[[], bool] | None = None
    ) -> bool:
        """Blocking put; polls ``abort`` while full so a dead or stopping
        consumer cannot wedge the producer forever. Returns False if aborted."""
        while True:
            try:
                self._q.put(item, timeout=0.5)
                return True
            except queue.Full:
                if abort is not None and abort():
                    return False

    def get(self, timeout: float | None = None) -> Any:
        return self._q.get(timeout=timeout)

    def qsize(self) -> int:
        return self._q.qsize()
