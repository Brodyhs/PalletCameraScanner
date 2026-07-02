"""SegmentRecorder: persist motion-segment bursts off the pipeline thread.

Trial recording mode (Phase 6.1, default OFF). A dedicated daemon thread
drains a bounded queue and writes each submitted burst through its OWN
:class:`EvidenceWriter` into the recording directory (disjoint from miss
evidence). Submission is **non-blocking drop-newest**: the pipeline thread
must never stall on recording I/O, so when the queue is full the newest
burst is dropped and counted rather than applying backpressure — the exact
opposite of :class:`~palletscan.events.bus.EventBus`, whose events are the
product and therefore block.

Honesty note on the recorded label: in a live trial the only ground truth
is the live decode outcome, so a recorded ``outcome: "miss"`` with
``payloads: []`` is a genuine no-read at capture time; a later replay
"recovery" over such a burst is a candidate, never confirmed truth.

Mirrors ``events/bus.py``'s thread pattern: sentinel shutdown with a
bounded join; work still queued at a wedged-writer timeout is counted, not
fatal.

Single-producer (the pipeline thread calls :meth:`submit`) / single-consumer
(the recorder thread); the counters each have exactly one writer thread.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from palletscan.config import RecordingConfig
from palletscan.events.evidence import EvidenceWriter
from palletscan.reliability.queues import SENTINEL
from palletscan.types import Frame

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RecordJob:
    candidate_id: str
    frames: list[Frame]
    meta: dict[str, Any] = field(default_factory=dict)


class SegmentRecorder:
    """Off-thread writer for recorded motion-segment bursts."""

    def __init__(self, cfg: RecordingConfig, join_timeout_s: float = 10.0) -> None:
        # Constructing the writer creates the recording dir; that is why the
        # recorder is only ever built when recording is enabled (default OFF
        # means None at the call site, so no directory is created).
        self._writer = EvidenceWriter(cfg.evidence)
        self.queue: queue.Queue = queue.Queue(maxsize=cfg.queue_maxsize)
        self._join_timeout_s = join_timeout_s
        self._thread = threading.Thread(
            target=self._run, name="segment-recorder", daemon=True
        )
        #: Bursts accepted onto the queue.
        self.enqueued = 0
        #: Bursts dropped because the queue was full (pipeline never blocks).
        self.dropped = 0
        #: Bursts written whole (EvidenceRef with no error).
        self.written = 0
        #: Bursts whose write raised or degraded (flagged EvidenceRef).
        self.write_failures = 0

    def start(self) -> None:
        self._thread.start()

    def submit(
        self, candidate_id: str, frames: list[Frame], meta: dict[str, Any]
    ) -> None:
        """Non-blocking hand-off. Drops (and counts) the newest burst when
        the queue is full so the pipeline thread never stalls on recording."""
        try:
            self.queue.put_nowait(_RecordJob(candidate_id, list(frames), meta))
            self.enqueued += 1
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 64 == 0:
                log.warning(
                    "segment recorder queue full; dropped newest burst for %s "
                    "(%d dropped so far)",
                    candidate_id,
                    self.dropped,
                )

    def _run(self) -> None:
        while True:
            job = self.queue.get()
            if job is SENTINEL:
                break
            assert isinstance(job, _RecordJob)
            try:
                ref = self._writer.write_burst(job.candidate_id, job.frames, job.meta)
            except Exception:
                # write_burst degrades on OSError itself; this layer catches
                # anything else so a single bad burst cannot kill the worker
                # thread and silently end all recording.
                self.write_failures += 1
                log.exception(
                    "segment recorder: burst for %s failed; worker continues",
                    job.candidate_id,
                )
                continue
            if ref.error is not None:
                self.write_failures += 1
            else:
                self.written += 1

    def shutdown(self) -> bool:
        """Drain the queue, then join the thread. Returns True when fully
        drained; False (non-fatal) means the writer is wedged and some
        bursts die with the daemon at interpreter exit."""
        # Hand the sentinel off with a BOUNDED wait, never a plain blocking
        # put. submit() is non-blocking drop-newest, so a wedged writer leaves
        # the bounded queue full indefinitely; a blocking put(SENTINEL) would
        # then wait forever for a slot the wedged consumer never frees — that
        # would starve the join / return-False degradation below, the very
        # path that exists to survive a wedged writer. On timeout the sentinel
        # simply never lands; the join then reports the wedge and returns
        # False (the daemon dies with its in-flight burst at interpreter exit).
        try:
            self.queue.put(SENTINEL, timeout=self._join_timeout_s)
        except queue.Full:
            pass
        self._thread.join(timeout=self._join_timeout_s)
        if self._thread.is_alive():
            undrained = sum(
                1 for item in list(self.queue.queue) if item is not SENTINEL
            )
            log.error(
                "segment recorder did not stop within %.0fs (wedged writer?); "
                "~%d recorded burst(s) will be lost at exit",
                self._join_timeout_s,
                undrained,
            )
            return False
        return True
