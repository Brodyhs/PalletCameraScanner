"""Probing: candidate matrices, set/readback honesty, choose_mode ranking."""

from __future__ import annotations

import cv2
import pytest

from palletscan.sources.controls import fourcc_float
from palletscan.sources.probe import (
    ModeCandidate,
    ProbeResult,
    candidates_for,
    choose_mode,
    format_probe_table,
    probe_modes,
)
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock


def _result(
    fourcc: str = "YUY2",
    width: int = 1920,
    height: int = 1200,
    requested_fps: float = 60.0,
    achieved: float | None = 60.0,
    opened: bool = True,
) -> ProbeResult:
    return ProbeResult(
        ModeCandidate(fourcc, width, height, requested_fps),
        opened=opened,
        actual_fourcc=fourcc if opened else None,
        actual_width=width if opened else None,
        actual_height=height if opened else None,
        achieved_fps=achieved if opened else None,
        frames_sampled=int(achieved or 0),
    )


# -- candidates_for -------------------------------------------------------------


def test_candidates_for_known_devices() -> None:
    color = candidates_for("See3CAM_24CUG")
    assert all((c.width, c.height) == (1920, 1200) for c in color)
    assert {c.fps for c in color} == {120.0, 60.0, 30.0}
    assert {c.fourcc for c in color} == {"UYVY", "YUY2", "MJPG"}
    assert len(color) == 9
    mono = candidates_for("see3cam_37cugm (e-con)")  # case/extra text tolerated
    assert all((c.width, c.height) == (2064, 1552) for c in mono)
    assert "GREY" in {c.fourcc for c in mono}
    assert {c.fps for c in mono} == {72.0, 60.0, 30.0}


def test_candidates_for_generic_seeds_current_mode_first() -> None:
    current = ModeCandidate("NV12", 800, 600, 15.0)
    cands = candidates_for("Some Random Webcam", current=current)
    assert cands[0] == current
    assert ModeCandidate("MJPG", 640, 480, 30.0) in cands
    # No duplicate when the current mode is already in the matrix.
    dup = ModeCandidate("YUY2", 1280, 720, 30.0)
    assert candidates_for("Generic", current=dup).count(dup) == 1


# -- probe_modes -----------------------------------------------------------------


def test_probe_modes_fresh_capture_per_candidate_and_readback() -> None:
    clock = FakeClock()
    # Device sustains 118 fps on MJPG but only 40 uncompressed, and snaps
    # any non-1920 width back to 1920.
    def make_fake(index: int, backend: int) -> FakeCapture:
        def real_fps(cap: FakeCapture) -> float:
            mjpg = cap.get(cv2.CAP_PROP_FOURCC) == fourcc_float("MJPG")
            return 118.0 if mjpg else 40.0

        return FakeCapture(
            hooks={cv2.CAP_PROP_FRAME_WIDTH: lambda v: 1920.0},
            clock=clock,
            real_fps=real_fps,
        )

    factory = FakeCaptureFactory(default=make_fake)
    cands = [
        ModeCandidate("UYVY", 1920, 1200, 120.0),
        ModeCandidate("MJPG", 1280, 1200, 120.0),
    ]
    results = probe_modes(
        lambda: factory(0, 0), cands, sample_s=0.5, warmup_frames=2, clock=clock
    )
    assert len(factory.created) == 2  # fresh capture per candidate
    assert all(c.release_calls == 1 for c in factory.created)
    uyvy, mjpg = results
    assert uyvy.achieved_fps == pytest.approx(40.0, rel=0.06)
    assert mjpg.achieved_fps == pytest.approx(118.0, rel=0.06)
    assert mjpg.actual_width == 1920  # snap caught by readback
    assert mjpg.actual_fourcc == "MJPG"
    assert uyvy.frames_sampled > 0


def test_probe_modes_records_open_failures_and_exceptions() -> None:
    clock = FakeClock()
    boom = FakeCapture(clock=clock, real_fps=30.0)
    boom.hooks[cv2.CAP_PROP_FOURCC] = lambda v: (_ for _ in ()).throw(
        RuntimeError("driver crash")
    )
    factory = FakeCaptureFactory(
        captures=[FakeCapture(opened=False), boom],
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0),
    )
    cands = [ModeCandidate("YUY2", 640, 480, 30.0)] * 3
    results = probe_modes(
        lambda: factory(0, 0), cands, sample_s=0.2, warmup_frames=0, clock=clock
    )
    assert not results[0].opened and "open" in (results[0].error or "")
    assert not results[1].opened and "driver crash" in (results[1].error or "")
    assert results[2].opened  # later candidates unaffected by earlier failures
    assert all(c.release_calls == 1 for c in factory.created)


# -- choose_mode -----------------------------------------------------------------


def test_choose_mode_filters_fps_shortfall() -> None:
    results = [
        _result("UYVY", requested_fps=120.0, achieved=50.0),  # 42% of asked
        _result("UYVY", requested_fps=60.0, achieved=59.0),
    ]
    chosen = choose_mode(results)
    assert chosen is results[1]


def test_choose_mode_prefers_full_resolution_over_fps() -> None:
    results = [
        _result("YUY2", 640, 480, requested_fps=120.0, achieved=120.0),
        _result("YUY2", 1920, 1200, requested_fps=30.0, achieved=30.0),
    ]
    assert choose_mode(results) is results[1]


def test_choose_mode_prefers_uncompressed_among_near_equals() -> None:
    results = [
        _result("MJPG", requested_fps=120.0, achieved=120.0),
        _result("UYVY", requested_fps=120.0, achieved=117.0),  # within 5%
    ]
    assert choose_mode(results) is results[1]


def test_choose_mode_accepts_mjpg_when_uncompressed_cannot_sustain() -> None:
    results = [
        _result("UYVY", requested_fps=120.0, achieved=40.0),  # filtered out
        _result("MJPG", requested_fps=120.0, achieved=118.0),
    ]
    assert choose_mode(results) is results[1]


def test_choose_mode_none_when_nothing_qualifies() -> None:
    assert choose_mode([]) is None
    assert (
        choose_mode(
            [
                _result(opened=False),
                _result(requested_fps=120.0, achieved=10.0),
            ]
        )
        is None
    )


def test_format_probe_table_lists_all_and_marks_choice() -> None:
    results = [
        _result("UYVY", requested_fps=120.0, achieved=117.0),
        _result(opened=False),
    ]
    chosen = choose_mode(results)
    table = format_probe_table(results, chosen)
    assert "UYVY 1920x1200@120" in table
    assert "CHOSEN" in table
    assert "did not open" in table
