"""EventBus: a dedicated thread that fans events out to sinks.

Events are the product — the input queue uses blocking puts (never drops).
A failing sink is logged and isolated so the other sinks keep receiving.
"""

from __future__ import annotations

import logging
import queue
import threading

from palletscan.events.sinks import Sink
from palletscan.types import Event

log = logging.getLogger(__name__)

#: End-of-stream marker for queues (identity-compared).
SENTINEL: object = object()


class EventBus:
    def __init__(self, sinks: list[Sink], maxsize: int = 1024) -> None:
        self._sinks = sinks
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(
            target=self._run, name="eventbus", daemon=True
        )
        self.events_handled = 0
        self.sink_errors = 0

    def start(self) -> None:
        self._thread.start()

    def publish(self, event: Event) -> None:
        """Blocking put — backpressure stalls the pipeline, never drops."""
        self.queue.put(event)

    def shutdown(self) -> None:
        """Drain remaining events, close sinks, join the thread."""
        self.queue.put(SENTINEL)
        self._thread.join(timeout=30)
        if self._thread.is_alive():  # pragma: no cover - defensive
            log.error("eventbus thread did not stop within 30s")

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
