"""Camera-free owner-thread lifecycle tests for PyGrabberCapture.

A FAKE ``pygrabber.dshow_graph`` (FilterGraph/FilterType) and a fake
``comtypes`` are installed into ``sys.modules`` BEFORE the capture is built, so
the real owner thread runs end-to-end against fakes: build the graph, arm the
grabber, wait for a frame on the liveness condition, then serve teardown. No
hardware, no real COM.

comtypes IS installed on this Windows box, but the suite must still collect on
machines without it (the module is imported at the top of pygrabber_capture),
so we guard with ``importorskip``.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("comtypes")

import cv2  # noqa: E402

from palletscan.sources.pygrabber_capture import PyGrabberCapture  # noqa: E402
from tests.pygrabber_fakes import (  # noqa: E402
    FakeFilterGraph,
    fake_directshow,  # noqa: F401 - pytest fixture
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
