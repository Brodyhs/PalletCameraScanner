"""Camera-free owner-thread lifecycle tests for PyGrabberCapture.

A FAKE ``pygrabber.dshow_graph`` (FilterGraph/FilterType) is installed into
``sys.modules`` and a fake ``comtypes`` is swapped onto the capture module's
``comtypes`` ATTRIBUTE (the owner thread resolves CoInitialize through its
module globals, so a ``sys.modules`` swap alone would be dead code) BEFORE the
capture is built. The real owner thread then runs end-to-end against fakes:
build the graph, arm the grabber, wait for a frame on the liveness condition,
serve control commands, then tear down. No hardware, no real COM.

comtypes IS installed on this Windows box, but the suite must still collect on
machines without it (the module is imported at the top of pygrabber_capture),
so we guard with ``importorskip``.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

pytest.importorskip("comtypes")

import cv2  # noqa: E402

from palletscan.sources import dshow_controls  # noqa: E402
from palletscan.sources import pygrabber_capture as pygrabber_capture_mod  # noqa: E402
from palletscan.sources.pygrabber_capture import PyGrabberCapture  # noqa: E402
from tests.pygrabber_fakes import (  # noqa: E402
    FakeCamCtrl,
    FakeControlFilter,
    FakeFilterGraph,
    FakeProcAmp,
    FakeSampleGrabber,
    fake_directshow,  # noqa: F401 - pytest fixture
    install_fake_directshow,
)

_Y8 = [{"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48}]


def _frame(*, mono: bool) -> np.ndarray:
    # SampleGrabber forces RGB24, so the callback always receives a 3-D array;
    # for a mono sensor the three channels are replicated luma.
    return np.full((48, 64, 3), 120, np.uint8)


# -- happy path --------------------------------------------------------------


def test_happy_path_opens_and_streams(
    fake_directshow, caplog: pytest.LogCaptureFixture
) -> None:
    graph = FakeFilterGraph(formats=_Y8, frames=[_frame(mono=True)])
    fake_directshow(graph)

    with caplog.at_level("INFO"):
        cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)

    assert cap.isOpened() is True
    assert cap._build_error is None
    assert any("streaming" in r.message for r in caplog.records)
    # A frame is readable through the Capture protocol.
    ok, img = cap.read()
    assert ok and img is not None


# -- mono single-channel delivery (NEW behavior) -----------------------------


def test_mono_three_channel_frame_published_as_2d(fake_directshow) -> None:
    """For a mono fourcc, _on_frame collapses the replicated-luma RGB24 to a
    single contiguous channel so the published frame is 2-D at ingest."""
    graph = FakeFilterGraph(formats=_Y8, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)

    assert cap.isOpened()
    assert cap._mono is True
    ok, img = cap.read()
    assert ok and img is not None
    assert img.ndim == 2  # collapsed to one luma channel
    assert img.shape == (48, 64)
    assert img.flags["C_CONTIGUOUS"]


def test_non_mono_three_channel_frame_left_3d(fake_directshow) -> None:
    """A true-color format on this backend is NOT collapsed (self._mono False);
    to_gray's cvtColor handles it downstream, so the 3-D frame is preserved."""
    color = [{"index": 0, "media_type_str": "YUY2", "width": 64, "height": 48}]
    graph = FakeFilterGraph(formats=color, frames=[_frame(mono=False)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, prefer_y8=False, open_timeout_s=2.0)
    fake_directshow.register(cap)

    assert cap.isOpened()
    assert cap._mono is False
    ok, img = cap.read()
    assert ok and img is not None
    assert img.ndim == 3  # left 3-channel for downstream to_gray


# -- liveness timeout (graph builds but never delivers a frame) --------------


def test_liveness_timeout_not_opened_with_build_error(fake_directshow) -> None:
    graph = FakeFilterGraph(formats=_Y8, frames=[])  # dead graph: no frames
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=0.3)
    fake_directshow.register(cap)

    assert cap.isOpened() is False
    assert cap._build_error is not None
    assert "no frame" in cap._build_error


# -- build failure (a graph step raises) -------------------------------------


def test_build_failure_not_opened_with_build_error(fake_directshow) -> None:
    graph = FakeFilterGraph(
        formats=_Y8, frames=[_frame(mono=True)], raise_on="prepare_preview_graph"
    )
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=1.0)
    fake_directshow.register(cap)

    assert cap.isOpened() is False
    assert cap._build_error is not None  # repr() of the raised exception


def test_no_matching_format_is_build_failure(fake_directshow) -> None:
    # No Y8 and no target-res match -> _select_format raises PyGrabberCaptureError.
    bad = [{"index": 0, "media_type_str": "YUY2", "width": 800, "height": 600}]
    graph = FakeFilterGraph(formats=bad, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=1.0)
    fake_directshow.register(cap)

    assert cap.isOpened() is False
    assert cap._build_error is not None


# -- teardown / release ------------------------------------------------------


def test_release_is_idempotent(fake_directshow) -> None:
    graph = FakeFilterGraph(formats=_Y8, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)

    assert cap.isOpened()
    cap.release()
    assert cap.isOpened() is False
    # Second release must not raise (idempotent teardown).
    cap.release()
    assert cap.isOpened() is False
    # read() on a released capture returns no frame.
    assert cap.read() == (False, None)


# -- _on_frame post-stop guard -----------------------------------------------


def test_on_frame_post_stop_does_not_publish(fake_directshow) -> None:
    """Once _stop is set, _on_frame returns immediately without bumping the
    sequence (the teardown-race guard, CAM-01)."""
    graph = FakeFilterGraph(formats=_Y8, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()

    cap._stop.set()
    seq_before = cap._seq
    cap._on_frame(_frame(mono=True))
    assert cap._seq == seq_before  # nothing published after stop


# -- constructor-timeout lifecycle (REVIEW finding 11) -------------------------


def test_constructor_timeout_abandoned_thread_never_runs_graph(
    fake_directshow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the constructor gives up, the abandoned owner thread must bail at
    its next _stop check: no graph.run() (which would stream the EXCLUSIVE UVC
    device), no late _opened=True resurrection, and a clean teardown."""
    monkeypatch.setattr(pygrabber_capture_mod, "_READY_GRACE_S", 0.2)
    graph = FakeFilterGraph(
        formats=_Y8, frames=[_frame(mono=True)], block_on="prepare_preview_graph"
    )
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=0.2)
    fake_directshow.register(cap)
    assert cap.isOpened() is False  # constructor timed out
    assert cap._stop.is_set()

    graph.unblock.set()  # the "wedged" COM call finally returns
    cap._thread.join(timeout=3.0)
    assert not cap._thread.is_alive()  # owner thread exited, not leaked
    assert graph.run_called is False  # never streamed the exclusive device
    assert cap.isOpened() is False  # _opened never overwritten to True
    assert graph.stopped and graph.removed  # torn down on the owner thread


def test_release_join_timeout_is_bounded_and_logged(
    fake_directshow, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged owner thread cannot hang release(): the join is bounded and
    the abandonment is loud (the unavoidable wedged-COM case)."""
    monkeypatch.setattr(pygrabber_capture_mod, "_READY_GRACE_S", 0.2)
    graph = FakeFilterGraph(
        formats=_Y8, frames=[_frame(mono=True)], block_on="prepare_preview_graph"
    )
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=0.2)
    fake_directshow.register(cap)
    assert cap.isOpened() is False
    with caplog.at_level("WARNING"):
        cap.release()  # owner thread still blocked inside the fake COM call
    assert any("wedged COM call" in r.message for r in caplog.records)
    graph.unblock.set()  # let the thread exit so the fixture can clean up
    cap._thread.join(timeout=3.0)


# -- CoUninitialize ordering (REVIEW finding 11b) ------------------------------


def test_com_refs_dropped_before_couninitialize(fake_directshow) -> None:
    """_run must drop its graph/dev COM refs BEFORE CoUninitialize — comtypes
    Release()s from __del__, and releasing after the apartment is torn down is
    undefined per COM rules. The spy inspects _run's finally-block locals."""
    graph = FakeFilterGraph(formats=_Y8, frames=[_frame(mono=True)])
    fake_directshow(graph)

    fake_ct = pygrabber_capture_mod.comtypes
    assert getattr(fake_ct, "_palletscan_fake", False)
    seen: dict[str, object] = {}
    orig_uninit = fake_ct.CoUninitialize

    def spy() -> None:
        frame = sys._getframe(1)  # _run's frame, inside its finally block
        seen["graph"] = frame.f_locals.get("graph")
        seen["dev"] = frame.f_locals.get("dev")
        orig_uninit()

    fake_ct.CoUninitialize = spy

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()
    cap.release()
    cap._thread.join(timeout=3.0)

    assert "graph" in seen  # CoUninitialize was reached
    assert seen["graph"] is None  # graph ref dropped first
    assert seen["dev"] is None  # device IBaseFilter ref dropped first


# -- fake-comtypes interception (REVIEW finding: tests/pygrabber_fakes 332) ----


def test_owner_thread_uses_fake_comtypes_not_real_com(fake_directshow) -> None:
    """The owner thread must resolve CoInitialize/CoUninitialize through the
    swapped module ATTRIBUTE — proving the lifecycle suite runs NO real COM."""
    graph = FakeFilterGraph(formats=_Y8, frames=[_frame(mono=True)])
    fake_directshow(graph)

    fake_ct = pygrabber_capture_mod.comtypes
    assert getattr(fake_ct, "_palletscan_fake", False)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()
    cap.release()
    cap._thread.join(timeout=3.0)

    assert fake_ct.calls["init"] >= 1  # the FAKE was hit, not real COM
    assert fake_ct.calls["uninit"] >= 1


def test_double_install_is_idempotent_and_restores_true_state() -> None:
    """A second install must not re-snapshot the first install's fakes as
    'saved'; one uninstall restores the TRUE pre-fake state and stale handles
    are no-ops (REVIEW finding: tests/pygrabber_fakes 366)."""
    real_pg = sys.modules.get("pygrabber")
    real_graph_mod = sys.modules.get("pygrabber.dshow_graph")
    real_ct_attr = pygrabber_capture_mod.comtypes

    u1 = install_fake_directshow(lambda: FakeFilterGraph())
    try:
        u2 = install_fake_directshow(lambda: FakeFilterGraph())
        u2()
        assert sys.modules.get("pygrabber") is real_pg
        assert sys.modules.get("pygrabber.dshow_graph") is real_graph_mod
        assert pygrabber_capture_mod.comtypes is real_ct_attr
    finally:
        u1()  # stale handle after u2 restored: must be a harmless no-op
    assert sys.modules.get("pygrabber") is real_pg
    assert pygrabber_capture_mod.comtypes is real_ct_attr


# -- one-shot re-arm contract (REVIEW finding: tests/pygrabber_fakes 271) ------


def test_fake_grabber_is_one_shot_and_clears_before_callback() -> None:
    """The fake must mirror the real BufferCB: unarmed buffers are dropped,
    and keep_photo is cleared BEFORE the callback runs (one-shot)."""
    armed_at_callback: list[bool] = []
    grabber = FakeSampleGrabber(
        lambda img: armed_at_callback.append(grabber.keep_photo)
    )
    frame = _frame(mono=True)

    assert grabber.deliver(frame) is False  # unarmed -> dropped
    assert armed_at_callback == []

    grabber.keep_photo = True
    assert grabber.deliver(frame) is True
    assert armed_at_callback == [False]  # cleared before the callback fired


def test_capture_rearms_continuously_so_frames_keep_flowing(
    fake_directshow,
) -> None:
    """Frame N+1 is only deliverable if _on_frame re-armed after frame N —
    pumping the whole scripted list proves the re-arm contract that keeps
    frames flowing on real hardware."""
    frames = [_frame(mono=True), _frame(mono=True), _frame(mono=True)]
    graph = FakeFilterGraph(formats=_Y8, frames=frames)
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()

    graph.join_pump(timeout=3.0)
    assert graph.delivered == len(frames)  # every frame needed a fresh re-arm
    assert cap._seq == len(frames)


# -- real _command_loop control marshaling (REVIEW finding: marshal 128) -------

_Y8_CTRL = [{"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48}]


def test_control_set_drives_real_command_loop_on_owner_thread(
    fake_directshow,
) -> None:
    """set() marshals a control command through the REAL _command_loop running
    on the real owner thread (fakes expose a working QueryInterface path)."""
    amp = FakeProcAmp.with_gain(mn=0, mx=100, default=0)
    cam = FakeCamCtrl.with_exposure(mn=-13, mx=-1, default=-6)
    filt = FakeControlFilter(cam_ctrl=cam, proc_amp=amp)
    graph = FakeFilterGraph(
        formats=_Y8_CTRL, frames=[_frame(mono=True)], instance=filt
    )
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()
    assert cap._has_controls is True  # QueryInterface path worked

    assert cap.set(cv2.CAP_PROP_GAIN, 42.0) is True
    assert cap.get(cv2.CAP_PROP_GAIN) == 42.0
    assert amp.set_calls[-1][0] == dshow_controls.VideoProcAmp_Gain

    assert cap.set(cv2.CAP_PROP_EXPOSURE, -6.0) is True
    assert cam.set_calls[-1][:2] == (dshow_controls.CameraControl_Exposure, -6)


def test_auto_exposure_without_camera_control_reports_failure(
    fake_directshow,
) -> None:
    """No IAMCameraControl -> the AE flag cannot reach hardware, so set() must
    return False through the real loop (REVIEW finding: pygrabber_capture 293,
    which echoed the sentinel as a fabricated success)."""
    filt = FakeControlFilter(proc_amp=FakeProcAmp.with_gain())  # no cam_ctrl
    graph = FakeFilterGraph(
        formats=_Y8_CTRL, frames=[_frame(mono=True)], instance=filt
    )
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()
    assert cap._has_controls is True  # proc-amp present, so the gate is open

    assert cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0) is False
    assert cap.get(cv2.CAP_PROP_AUTO_EXPOSURE) == 0.0  # nothing cached


# -- fps honesty end-to-end (REVIEW finding 8) ---------------------------------


def test_fps_honored_when_capability_is_fixed_rate(fake_directshow) -> None:
    fmts = [{"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48,
             "min_framerate": 72.0, "max_framerate": 72.0}]
    graph = FakeFilterGraph(formats=fmts, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()

    assert cap.set(cv2.CAP_PROP_FPS, 72.0) is True  # device really runs 72
    assert cap.get(cv2.CAP_PROP_FPS) == 72.0
    assert cap.set(cv2.CAP_PROP_FPS, 30.0) is False  # 30 was never programmed


def test_fps_rejected_when_capability_rate_unknown(fake_directshow) -> None:
    """A ranged capability's default AvgTimePerFrame is not exposed by
    pygrabber and never programmed: set() must reject (pre-fix it stored the
    value and get() echoed it — a fabricated verified fps)."""
    fmts = [{"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48,
             "min_framerate": 72.0, "max_framerate": 30.0}]  # pygrabber order
    graph = FakeFilterGraph(formats=fmts, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()

    assert cap.set(cv2.CAP_PROP_FPS, 60.0) is False  # honest rejection
    assert cap.get(cv2.CAP_PROP_FPS) == 0.0  # unknown, never a mirror


def test_constructor_fps_selects_matching_capability(fake_directshow) -> None:
    fmts = [
        {"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48,
         "min_framerate": 72.0, "max_framerate": 72.0},
        {"index": 1, "media_type_str": "Y8  ", "width": 64, "height": 48,
         "min_framerate": 30.0, "max_framerate": 30.0},
    ]
    graph = FakeFilterGraph(formats=fmts, frames=[_frame(mono=True)])
    fake_directshow(graph)

    cap = PyGrabberCapture(0, width=64, height=48, fps=30.0, open_timeout_s=2.0)
    fake_directshow.register(cap)
    assert cap.isOpened()

    assert graph._device is not None and graph._device.format_index == 1
    assert cap.get(cv2.CAP_PROP_FPS) == 30.0
    assert cap.set(cv2.CAP_PROP_FPS, 30.0) is True
