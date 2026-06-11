"""MotionGate: segment lifecycle, debounce, ROI correctness."""

from __future__ import annotations

import numpy as np
import pytest

from palletscan.config import MotionAlgorithm, MotionConfig
from palletscan.pipeline.motion_gate import MotionGate
from palletscan.types import Frame, SegmentKind


def _frame(image: np.ndarray, idx: int) -> Frame:
    return Frame(image=image, ts=idx / 30.0, frame_index=idx, source_id="cam0")


def _moving_square_stream(
    n: int, start: int = 0, stop: int | None = None, size: int = 120
) -> list[np.ndarray]:
    """n frames of 640x360; a bright square moves during [start, stop)."""
    stop = n if stop is None else stop
    frames = []
    bg = np.full((360, 640), 90, np.uint8)
    for i in range(n):
        img = bg.copy()
        if start <= i < stop:
            x = 40 + (i - start) * 25
            if x + size < 640:
                img[120 : 120 + size, x : x + size] = 220
        frames.append(img)
    return frames


def _run(gate: MotionGate, images: list[np.ndarray]):
    results, events = [], []
    for i, img in enumerate(images):
        res, ev = gate.update(_frame(img, i))
        results.append(res)
        if ev:
            events.append(ev)
    tail = gate.flush()
    if tail:
        events.append(tail)
    return results, events


def test_static_stream_produces_no_candidates() -> None:
    gate = MotionGate(MotionConfig(), "cam0")
    results, events = _run(gate, _moving_square_stream(30, start=99))
    assert events == []
    assert all(not r.active for r in results)


def test_single_pass_yields_one_segment_with_sane_bounds() -> None:
    cfg = MotionConfig()
    gate = MotionGate(cfg, "cam0")
    images = _moving_square_stream(60, start=10, stop=28)
    _, events = _run(gate, images)
    assert [e.kind for e in events] == [SegmentKind.OPEN, SegmentKind.CLOSE]
    opened, closed = events
    assert opened.candidate_id == closed.candidate_id == "cam0-000001"
    # open is backdated to the first active frame (within a frame or two of
    # actual motion start; frame differencing sees motion at start+1)
    assert 10 <= opened.frame_index <= 12
    # close carries the last active frame, not the quiet tail
    assert 27 <= closed.frame_index <= 29


def test_roi_contains_the_moving_object() -> None:
    gate = MotionGate(MotionConfig(), "cam0")
    images = _moving_square_stream(30, start=5, stop=25)
    results, _ = _run(gate, images)
    active = [r for r in results if r.active]
    assert active
    for i, img in enumerate(images):
        res = gate_res = results[i]
        if not res.active or res.roi is None:
            continue
        ys, xs = np.nonzero(img > 150)
        if len(xs) == 0:
            continue
        roi = res.roi
        assert roi.x <= xs.min() and xs.max() <= roi.x + roi.w
        assert roi.y <= ys.min() and ys.max() <= roi.y + roi.h


def test_one_frame_blip_does_not_open_segment() -> None:
    # A 1-frame blip yields 2 consecutive active diffs (appear + disappear);
    # the default open_frames=3 debounces it.
    gate = MotionGate(MotionConfig(), "cam0")
    bg = np.full((360, 640), 90, np.uint8)
    blip = bg.copy()
    blip[100:220, 200:320] = 220
    images = [bg, bg, blip, bg, bg, bg, bg, bg, bg, bg, bg, bg]
    _, events = _run(gate, images)
    assert events == []


def test_quiet_frames_close_timing() -> None:
    cfg = MotionConfig(quiet_frames=8)
    gate = MotionGate(cfg, "cam0")
    images = _moving_square_stream(50, start=5, stop=20)
    closes = []
    for i, img in enumerate(images):
        _, ev = gate.update(_frame(img, i))
        if ev and ev.kind is SegmentKind.CLOSE:
            closes.append((i, ev))
    assert len(closes) == 1
    emitted_at, ev = closes[0]
    # close decision is made quiet_frames after the last active frame
    assert emitted_at - ev.frame_index == pytest.approx(cfg.quiet_frames, abs=1)


def test_flush_closes_open_segment() -> None:
    gate = MotionGate(MotionConfig(), "cam0")
    images = _moving_square_stream(15, start=5)  # never goes quiet
    _, events = _run(gate, images)  # _run calls flush()
    kinds = [e.kind for e in events]
    assert kinds == [SegmentKind.OPEN, SegmentKind.CLOSE]


def test_two_passes_get_distinct_candidate_ids() -> None:
    gate = MotionGate(MotionConfig(), "cam0")
    a = _moving_square_stream(40, start=5, stop=18)
    b = _moving_square_stream(40, start=12, stop=25)
    _, events = _run(gate, a + b)
    ids = {e.candidate_id for e in events}
    assert len(ids) == 2


def test_mog2_mode_smoke() -> None:
    cfg = MotionConfig(algorithm=MotionAlgorithm.MOG2)
    gate = MotionGate(cfg, "cam0")
    images = _moving_square_stream(60, start=20, stop=40)
    _, events = _run(gate, images)
    assert any(e.kind is SegmentKind.OPEN for e in events)
