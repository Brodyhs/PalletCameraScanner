"""WatchdogSource: stall detection and automatic reopen for live sources.

A generic wrapper, deliberately *not* internal to CameraSource: detection
is testable against stalled synthetic sources, recovery against
CameraSource+fakes, and the runner's crash-only ``_source_loop`` needs a
``frames()`` that keeps yielding across reopens — this wrapper is where
that absorption lives. Only camera sources get wrapped; synthetic/video
paths are bit-identical to Phases 1–2.

Mechanics: a daemon **reader thread** runs ``inner.frames()`` into a
small handoff queue, every item tagged with a generation token. The
consumer (the runner's source thread, inside :meth:`frames`) blocks on
the queue with ``stall_timeout_s``: a timeout means the device stalled; a
reader exception arrives immediately (fast path, no timeout wait). Either
way recovery runs *on the consumer thread*: close the inner source
(``release()`` usually unblocks a hung ``read()``), join the old reader —
still alive means it is wedged inside the driver and is abandoned as a
**zombie** (its stale-generation output can never poison the stream) —
then jittered-backoff ``reopen()`` attempts (re-enumeration by name and
settings re-apply live inside ``CameraSource.reopen``). The backoff
attempt counter resets only when a frame is actually yielded.

The watchdog never gives up by default — process exit cannot fix an
unplugged camera. Two escalation valves raise :class:`WatchdogEscalation`
through the existing crash-only chain (source thread → ``_thread_errors``
→ ``run()`` raises → CLI exit code **3** → supervisor restart):
``max_zombie_readers`` (a growing pile of hung reader threads is the
wedged-USB-stack signature; only a process restart resets a wedged stack)
and ``max_outage_s`` (off by default).
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol, runtime_checkable

from palletscan.config import WatchdogConfig
from palletscan.sources.base import FrameSource
from palletscan.types import Frame

log = logging.getLogger(__name__)

_HANDOFF_MAXSIZE = 4
_MARKER_PUT_TIMEOUT_S = 1.0


class WatchdogEscalation(RuntimeError):
    """Recovery cannot make progress in-process; restart the process
    (exit code 3: "USB stack wedged, check cable/hub")."""


@runtime_checkable
class Reopenable(Protocol):
    """A source the watchdog knows how to recover."""

    def reopen(self) -> None: ...


class WatchdogSource(FrameSource):
    """Wraps a Reopenable FrameSource; absorbs failures behind ``frames()``.

    Counters are plain ints with a single writer (the consumer thread);
    the metrics registry reads them lazily at snapshot time.
    """

    def __init__(
        self,
        inner: FrameSource,
        cfg: WatchdogConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        sleeper: Callable[[float], bool] | None = None,
        join_timeout_s: float = 5.0,
    ) -> None:
        if not isinstance(inner, Reopenable):
            raise TypeError(
                f"{type(inner).__name__} is not Reopenable; the watchdog "
                "can only wrap sources with a reopen() recovery hook"
            )
        self.inner = inner
        self._cfg = cfg
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._stop = threading.Event()
        #: Interruptible wait; returns True when stopping. Injectable so
        #: backoff-sequence tests run on a fake clock.
        self._sleeper = sleeper if sleeper is not None else self._stop.wait
        self._join_timeout_s = join_timeout_s
        self._handoff: queue.Queue = queue.Queue(maxsize=_HANDOFF_MAXSIZE)
        self._gen = 0
        self._reader: threading.Thread | None = None
        self._started = False
        self._attempt = 0  # resets only when a frame is actually yielded
        self._outage_start: float | None = None
        self.stalls_detected = 0
        self.reconnects = 0
        self.reopen_failures = 0
        self.zombie_readers = 0

    # -- FrameSource -------------------------------------------------------

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
        """Yield frames forever across reopens; strictly single-use toward
        the runner. Raises WatchdogEscalation when in-process recovery
        cannot help."""
        if self._started:
            raise RuntimeError("WatchdogSource.frames() is single-use")
        self._started = True
        self._spawn_reader()
        while not self._stop.is_set():
            try:
                kind, gen, payload = self._handoff.get(
                    timeout=self._cfg.stall_timeout_s
                )
            except queue.Empty:
                self.stalls_detected += 1
                self._recover(
                    f"no frame for {self._cfg.stall_timeout_s:.1f}s (stall)"
                )
                continue
            if gen != self._gen:
                continue  # stale generation: a zombie can never poison the stream
            if self._stop.is_set():
                break
            if kind == "frame":
                self._attempt = 0
                self._outage_start = None
                yield payload
            elif kind == "error":
                self._recover(f"reader failed: {payload!r}")
            else:  # "end" — a live stream ending on its own IS a failure
                self._recover("source stream ended unexpectedly")

    def close(self) -> None:
        """Graceful stop, callable from any thread: interrupts backoff
        waits (the sleeper observes ``_stop``), unblocks a hung read
        (``inner.close()``), frees a reader stuck in ``put`` (drain), and
        wakes a consumer blocked in its stall-wait ``get`` (marker) — a
        source mid-outage never yields, so without the wake-up a runner
        stopping during an outage would wait out the stall timeout."""
        self._stop.set()
        self.inner.close()
        self._drain()
        try:
            self._handoff.put_nowait(("stop", self._gen, None))
        except queue.Full:  # consumer is not waiting; nothing to wake
            pass
        reader = self._reader
        if reader is not None:
            reader.join(timeout=1.0)

    # -- reader thread -------------------------------------------------------

    def _spawn_reader(self) -> None:
        gen = self._gen
        it = self.inner.frames()
        t = threading.Thread(
            target=self._read_loop,
            args=(gen, it),
            name=f"watchdog-reader-{self.source_id}-g{gen}",
            daemon=True,
        )
        self._reader = t
        t.start()

    def _read_loop(self, gen: int, it: Iterator[Frame]) -> None:
        try:
            for frame in it:
                if self._stop.is_set() or gen != self._gen:
                    return
                self._handoff.put(("frame", gen, frame))
        except Exception as exc:  # device failure -> consumer fast path
            self._put_marker(("error", gen, exc))
        else:
            self._put_marker(("end", gen, None))

    def _put_marker(self, item: tuple) -> None:
        try:
            self._handoff.put(item, timeout=_MARKER_PUT_TIMEOUT_S)
        except queue.Full:  # consumer gone; stall timeout covers detection
            log.debug("handoff full; dropped %s marker", item[0])

    def _drain(self) -> None:
        while True:
            try:
                self._handoff.get_nowait()
            except queue.Empty:
                return

    # -- recovery (consumer thread) ---------------------------------------------

    def _recover(self, reason: str) -> None:
        if self._outage_start is None:
            self._outage_start = self._clock()
        log.warning("watchdog %s: %s; recovering", self.source_id, reason)
        # Release-first unblocking: closing the device is what frees a
        # reader stuck inside read().
        self.inner.close()
        self._reap_reader()
        while not self._stop.is_set():
            self._check_outage()
            self._attempt += 1
            delay = min(
                self._cfg.retry.cap_s,
                self._cfg.retry.base_s
                * 2 ** min(self._attempt - 1, 16)
                * self._rng.uniform(0.5, 1.5),
            )
            log.info(
                "watchdog %s: reopen attempt %d in %.2fs",
                self.source_id,
                self._attempt,
                delay,
            )
            if self._sleeper(delay) or self._stop.is_set():
                return  # close() during backoff exits promptly
            self._check_outage()
            try:
                self.inner.reopen()
            except Exception as exc:
                self.reopen_failures += 1
                log.warning(
                    "watchdog %s: reopen attempt %d failed: %r",
                    self.source_id,
                    self._attempt,
                    exc,
                )
                continue
            if self._stop.is_set():
                # close() raced the reopen: do not resurrect the device or
                # spawn a post-shutdown reader; release what reopen built.
                self.inner.close()
                return
            self._gen += 1
            self._drain()  # anything left belongs to dead generations
            self._spawn_reader()
            self.reconnects += 1
            log.info(
                "watchdog %s: source reopened (reconnect #%d, %d zombie "
                "reader(s) abandoned so far)",
                self.source_id,
                self.reconnects,
                self.zombie_readers,
            )
            return

    def _reap_reader(self) -> None:
        """Join the old reader, draining the handoff so a put cannot wedge
        it; still alive after the timeout = stuck inside the driver."""
        reader = self._reader
        self._reader = None
        if reader is None:
            return
        deadline = time.monotonic() + self._join_timeout_s
        while reader.is_alive() and time.monotonic() < deadline:
            self._drain()
            reader.join(timeout=0.05)
        if reader.is_alive():
            self.zombie_readers += 1
            log.error(
                "watchdog %s: reader thread stuck in read(); abandoned as "
                "zombie #%d",
                self.source_id,
                self.zombie_readers,
            )
            if self.zombie_readers > self._cfg.max_zombie_readers:
                raise WatchdogEscalation(
                    f"{self.zombie_readers} reader threads stuck in hung "
                    f"read() calls (max_zombie_readers="
                    f"{self._cfg.max_zombie_readers}): USB stack likely "
                    "wedged; only a process restart resets it"
                )

    def _check_outage(self) -> None:
        max_outage = self._cfg.max_outage_s
        if max_outage is None or self._outage_start is None:
            return
        outage = self._clock() - self._outage_start
        if outage > max_outage:
            raise WatchdogEscalation(
                f"source outage {outage:.1f}s exceeds max_outage_s="
                f"{max_outage:.1f}; escalating to process restart"
            )
