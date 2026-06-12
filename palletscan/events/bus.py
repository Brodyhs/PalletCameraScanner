"""EventBus: a dedicated thread that fans events out to sinks.

Events are the product — the input queue uses blocking puts (never drops).
A failing sink is logged and isolated so the other sinks keep receiving.
"""

from __future__ import annotations

import logging
import queue
import threading

from palletscan.events.sinks import Sink
from palletscan.reliability.queues import SENTINEL
from palletscan.types import Event

log = logging.getLogger(__name__)

__all__ = ["SENTINEL", "EventBus"]


class EventBus:
    def __init__(
        self,
        sinks: list[Sink],
        maxsize: int = 1024,
        join_timeout_s: float = 30.0,
    ) -> None:
        self._sinks = sinks
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._join_timeout_s = join_timeout_s
        self._thread = threading.Thread(
            target=self._run, name="eventbus", daemon=True
        )
        self.events_handled = 0
        self.sink_errors = 0
        #: Events still queued when shutdown gave up waiting — they die
        #: with the daemon thread at interpreter exit. Counted so callers
        #: can fail the run instead of printing a clean summary over a
        #: silent loss (REVIEW finding 11).
        self.events_lost = 0
        #: Events published after shutdown enqueued the SENTINEL (station
        #: mode: a straggling per-camera bus still submitting through the
        #: deduper). They are behind the SENTINEL and will never be
        #: handled; counted + logged, never silent.
        self.published_after_shutdown = 0
        self._shutdown_started = False

    def start(self) -> None:
        self._thread.start()

    def publish(self, event: Event) -> None:
        """Blocking put — backpressure stalls the pipeline, never drops."""
        if self._shutdown_started:
            self.published_after_shutdown += 1
            log.error(
                "event published after bus shutdown began; it is behind "
                "the SENTINEL and will NOT reach any sink (%d so far)",
                self.published_after_shutdown,
            )
        self.queue.put(event)

    def shutdown(self) -> bool:
        """Drain remaining events, close sinks, join the thread.

        Returns True when fully drained. False means the bus thread is
        wedged (a hung sink write): the remaining queue depth is counted
        in ``events_lost`` and logged, and the caller must treat the run
        as failed — exit 0 over a silent tail loss is exactly the failure
        mode this guards (REVIEW finding 11).
        """
        self._shutdown_started = True
        self.queue.put(SENTINEL)
        self._thread.join(timeout=self._join_timeout_s)
        if self._thread.is_alive():
            lost = sum(
                1 for item in list(self.queue.queue) if item is not SENTINEL
            )
            self.events_lost += lost
            log.error(
                "eventbus thread did not stop within %.0fs (wedged sink?); "
                "~%d queued event(s) will be lost at exit",
                self._join_timeout_s,
                lost,
            )
            return False
        return True

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is SENTINEL:
                break
            for sink in self._sinks:
                try:
                    sink.handle(item)
                except Exception:
                    self.sink_errors += 1
                    log.exception(
                        "sink %s failed; continuing", type(sink).__name__
                    )
            self.events_handled += 1
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:  # pragma: no cover - defensive
                log.exception("sink %s failed to close", type(sink).__name__)
