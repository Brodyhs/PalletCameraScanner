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
        res, evs = gate.update(_frame(img, i))
        results.append(res)
        events.extend(evs)
    events.extend(gate.flush())
    return results, events


def test_static_stream_produces_no_candidates() -> None:
    gate = MotionGate(MotionConfig(), "cam0")
    results, events = _run(gate, _moving_square_stream(30, start=99))
    assert events == []
    assert all(not r.active for r in results)


def test_single_pass_yields_one_segment_with_sane_bounds() -> None:
    cfg = MotionConfig()
    # run_token injected: candidate ids carry a per-run token so a restart
    # can never mint the same id again (REVIEW finding 5).
    gate = MotionGate(cfg, "cam0", run_token="t0")
    images = _moving_square_stream(60, start=10, stop=28)
    _, events = _run(gate, images)
    assert [e.kind for e in events] == [SegmentKind.OPEN, SegmentKind.CLOSE]
    opened, closed = events
    assert opened.candidate_id == closed.candidate_id == "cam0-t0-000001"
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
        _, evs = gate.update(_frame(img, i))
        for ev in evs:
            if ev.kind is SegmentKind.CLOSE:
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


def test_break_segment_closes_at_last_active_pre_gap_frame() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 2 (critical), gate half: a segment
    open when the source stalled stayed open across the outage and glued
    onto whatever motion was present at reconnect. break_segment() must
    close it at the last OBSERVED active frame — the pre-gap pallet."""
    gate = MotionGate(MotionConfig(), "cam0", run_token="t0")
    images = _moving_square_stream(20, start=5)  # motion never goes quiet
    opens = []
    for i, img in enumerate(images):
        _, evs = gate.update(_frame(img, i))
        opens.extend(evs)
    assert [e.kind for e in opens] == [SegmentKind.OPEN]
    broke_evs = gate.break_segment()
    assert len(broke_evs) == 1
    broke = broke_evs[0]
    assert broke.kind is SegmentKind.CLOSE
    assert broke.candidate_id == opens[0].candidate_id
    # Last active frame of the stream, not anything from after the gap.
    assert broke.frame_index <= 19
    assert broke.frame_index >= 17


def test_break_segment_resets_debounce_and_motion_model() -> None:
    """Finding 2, gate half: post-gap frames must re-warm like stream
    start. Without the model reset, diffing the first post-gap frame
    against the pre-gap reference manufactures phantom whole-frame motion
    that re-opens a segment instantly."""
    gate = MotionGate(MotionConfig(), "cam0", run_token="t0")
    for i, img in enumerate(_moving_square_stream(20, start=5)):
        gate.update(_frame(img, i))
    gate.break_segment()
    # Post-gap scene differs wholesale from the pre-gap reference (the
    # square is gone, background level shifted): quiet frames must yield
    # no candidate.
    bg = np.full((360, 640), 140, np.uint8)
    results = []
    for j in range(10):
        res, evs = gate.update(_frame(bg.copy(), 100 + j))
        results.append(res)
        assert all(ev.kind is not SegmentKind.OPEN for ev in evs)
    # Including the FIRST post-gap frame: with the model reset it is a
    # warm-up frame; without the reset it diffs against the pre-gap
    # reference and reads as whole-frame motion.
    assert all(not r.active for r in results), (
        "post-gap frames diffed against the pre-gap reference"
    )
    # Genuine post-gap motion opens a NEW candidate id.
    new_events = []
    for j, img in enumerate(_moving_square_stream(30, start=2, stop=20)):
        _, evs = gate.update(_frame(img, 200 + j))
        new_events.extend(evs)
    assert new_events and new_events[0].kind is SegmentKind.OPEN
    assert new_events[0].candidate_id.endswith("-000002")


def test_break_segment_recreates_mog2_background_model() -> None:
    """Finding 2 + design-review fix: 'MOG2 re-prime' must RECREATE the
    subtractor — merely skipping one frame keeps the pre-gap background
    model, and a legitimate post-reconnect brightness change (exposure
    re-negotiation is warn-and-continue) reads as whole-frame foreground
    for the model's entire history length."""
    cfg = MotionConfig(algorithm=MotionAlgorithm.MOG2)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    dark = np.full((360, 640), 90, np.uint8)
    for i in range(30):
        gate.update(_frame(dark.copy(), i))
    gate.break_segment()
    # Post-gap stream is globally brighter, no moving object.
    bright = np.full((360, 640), 140, np.uint8)
    for j in range(30):
        _, evs = gate.update(_frame(bright.copy(), 100 + j))
        assert evs == [], (
            "a global brightness step across the break opened a phantom "
            f"segment at post-gap frame {j}"
        )


def test_run_token_makes_candidate_ids_restart_unique() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 5: the per-process segment counter
    restarts at 0, so two same-day runs minted the same <source>-000001 id
    and their evidence directories silently merged. The run token keys
    them apart; the default token is time-derived."""
    import re

    images = _moving_square_stream(40, start=5, stop=18)
    ids = []
    for token in ("run1", "run2"):
        gate = MotionGate(MotionConfig(), "cam0", run_token=token)
        _, events = _run(gate, images)
        ids.append(events[0].candidate_id)
    assert ids == ["cam0-run1-000001", "cam0-run2-000001"]
    assert len(set(ids)) == 2
    default_gate = MotionGate(MotionConfig(), "cam0")
    _, events = _run(default_gate, images)
    assert re.fullmatch(r"cam0-\d{6}-000001", events[0].candidate_id)
