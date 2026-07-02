"""SegmentRecorder: off-thread burst writer — shutdown and drop-newest.

Patch-fixer coverage for the wedged-writer shutdown defect: a blocking
``put(SENTINEL)`` on the bounded queue could hang process shutdown forever
when the writer wedged and the queue stayed full (submit() is non-blocking
drop-newest, so it keeps the queue at maxsize). shutdown() must instead hand
the sentinel off with a BOUNDED wait and fall through to its non-fatal
return-False degradation.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from palletscan.config import EvidenceConfig, RecordingConfig
from palletscan.events.evidence import EvidenceRef
from palletscan.pipeline.segment_recorder import SegmentRecorder
from palletscan.types import Frame

_IMG = np.zeros((8, 8), np.uint8)


def _frame(i: int) -> Frame:
    return Frame(image=_IMG, ts=i / 10.0, frame_index=i, source_id="cam0")


def test_shutdown_does_not_block_on_wedged_full_queue(tmp_path, monkeypatch) -> None:
    """The defect: a wedged writer + full bounded queue made the blocking
    put(SENTINEL) hang shutdown() forever, starving the bounded join and its
    non-fatal return-False path. shutdown() must instead return (False) within
    a bounded time."""
    rec = SegmentRecorder(
        RecordingConfig(
            enabled=True,
            queue_maxsize=2,
            evidence=EvidenceConfig(dir=tmp_path / "rec", frame_stride=1),
        ),
        join_timeout_s=0.3,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_write(
        candidate_id: str, frames: list[Frame], meta: dict
    ) -> EvidenceRef:
        # Wedge the writer inside the current burst, exactly like a hung disk.
        started.set()
        release.wait()
        return EvidenceRef(directory=tmp_path, frame_count=len(frames))

    monkeypatch.setattr(rec._writer, "write_burst", blocking_write)
    rec.start()

    rec.submit("c0", [_frame(0)], {})  # dequeued -> writer wedges on it
    assert started.wait(2.0), "writer never picked up the first burst"
    # submit() is non-blocking drop-newest: it fills maxsize=2 then drops.
    for i in range(1, 6):
        rec.submit(f"c{i}", [_frame(i)], {})
    assert rec.queue.full()
    assert rec.dropped >= 1

    result: list[bool] = []
    caller = threading.Thread(target=lambda: result.append(rec.shutdown()))
    caller.start()
    caller.join(timeout=4.0)
    try:
        assert not caller.is_alive(), "shutdown() blocked on a wedged full queue"
        # Wedged writer -> non-fatal degradation, never True.
        assert result == [False]
    finally:
        release.set()  # unwedge so the daemon can be stopped cleanly
        caller.join(timeout=2.0)
        rec.shutdown()  # writer freed: now drains and joins the daemon


def test_submit_drops_newest_without_blocking_when_full(tmp_path) -> None:
    """submit() must never apply backpressure: with the writer thread never
    started the single slot fills and every further submit is an instant
    drop-and-count."""
    rec = SegmentRecorder(
        RecordingConfig(
            enabled=True,
            queue_maxsize=1,
            evidence=EvidenceConfig(dir=tmp_path / "rec", frame_stride=1),
        )
    )
    frame = _frame(0)
    rec.submit("c0", [frame], {})  # fills the only slot; never drained
    assert rec.enqueued == 1

    t0 = time.perf_counter()
    for i in range(1, 200):
        rec.submit(f"c{i}", [frame], {})
    elapsed = time.perf_counter() - t0

    assert rec.enqueued == 1
    assert rec.dropped == 199
    assert elapsed < 1.0  # 199 drop-newest submits are effectively instant


def test_shutdown_drains_pending_bursts_and_returns_true(tmp_path) -> None:
    """Happy path preserved: a live writer drains queued bursts and shutdown()
    reports a clean drain (True)."""
    rec = SegmentRecorder(
        RecordingConfig(
            enabled=True,
            evidence=EvidenceConfig(dir=tmp_path / "rec", frame_stride=1),
        ),
        join_timeout_s=5.0,
    )
    rec.start()
    rec.submit("c0", [_frame(0)], {"schema": "recording/v1", "outcome": "pass"})
    rec.submit("c1", [_frame(1)], {"schema": "recording/v1", "outcome": "miss"})
    assert rec.shutdown() is True
    assert rec.written == 2
    assert rec.dropped == 0
    assert rec.write_failures == 0
