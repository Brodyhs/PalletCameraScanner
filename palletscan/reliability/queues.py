"""Bounded queues with explicit backpressure policy."""

from __future__ import annotations

import dataclasses
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
        # Level-triggered discontinuity carry-over: drop-oldest is premised
        # on every frame being redundant, but the one frame marked
        # discontinuity=True is not — dropping it would silently lose the
        # segment-break signal under exactly the post-reconnect backlog
        # that produces drops (REVIEW finding 2). When a marked item is
        # discarded, the flag rides on the next item handed out instead.
        # Single-producer/single-consumer; bool flips are GIL-atomic.
        self._pending_discontinuity = False

    def put(self, item: Any) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    victim = self._q.get_nowait()
                    self.dropped += 1
                    if getattr(victim, "discontinuity", False):
                        self._pending_discontinuity = True
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
        item = self._q.get(timeout=timeout)
        if self._pending_discontinuity and hasattr(item, "discontinuity"):
            self._pending_discontinuity = False
            if not item.discontinuity:
                item = dataclasses.replace(item, discontinuity=True)
        return item

    def qsize(self) -> int:
        return self._q.qsize()
