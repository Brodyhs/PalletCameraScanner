"""RollingFrameBuffer: time eviction, extraction windows, memory bound."""

from __future__ import annotations

import numpy as np

from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.types import Frame

_IMG = np.zeros((4, 4), np.uint8)


def _frame(i: int, fps: float = 30.0) -> Frame:
    return Frame(image=_IMG, ts=i / fps, frame_index=i, source_id="s")


def test_time_based_eviction() -> None:
    buf = RollingFrameBuffer(horizon_s=1.0)
    for i in range(120):  # 4 seconds at 30 fps
        buf.append(_frame(i))
    # only the trailing ~1 s survives
    assert len(buf) <= 31
    oldest = buf.extract(0.0, 999.0)[0]
    assert oldest.ts >= 119 / 30.0 - 1.0 - 1e-9


def test_extract_returns_requested_window_inclusive() -> None:
    buf = RollingFrameBuffer(horizon_s=10.0)
    for i in range(60):
        buf.append(_frame(i))
    got = buf.extract(0.5, 1.0)
    assert [f.frame_index for f in got] == list(range(15, 31))


def test_extract_empty_window() -> None:
    buf = RollingFrameBuffer(horizon_s=10.0)
    for i in range(10):
        buf.append(_frame(i))
    assert buf.extract(5.0, 6.0) == []


def test_maxlen_bounds_memory() -> None:
    buf = RollingFrameBuffer(horizon_s=1e9, maxlen=50)
    for i in range(500):
        buf.append(_frame(i))
    assert len(buf) == 50
