"""Device enumeration by name: parsing, pairing, matching, fallbacks."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from palletscan.config import Backend
from palletscan.sources.devices import (
    DeviceInfo,
    backend_flag,
    devices_from_names,
    find_device,
    list_devices,
    parse_system_profiler,
    _list_macos,
)
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, dshow_hooks

_PROFILER_JSON = json.dumps(
    {
        "SPCameraDataType": [
            {"_name": "FaceTime HD Camera", "spcamera_model-id": "x"},
            {"_name": "See3CAM_24CUG", "spcamera_model-id": "y"},
        ]
    }
)


def _devs(*names: str, backend: int = 1200) -> list[DeviceInfo]:
    return devices_from_names(list(names), backend)


# -- parsing / pairing ---------------------------------------------------------


def test_parse_system_profiler_names() -> None:
    assert parse_system_profiler(_PROFILER_JSON) == [
        "FaceTime HD Camera",
        "See3CAM_24CUG",
    ]
    assert parse_system_profiler("{}") == []


def test_devices_from_names_pairs_enumeration_order() -> None:
    devs = devices_from_names(["A", "B"], int(cv2.CAP_DSHOW))
    assert devs == [
        DeviceInfo("A", 0, int(cv2.CAP_DSHOW)),
        DeviceInfo("B", 1, int(cv2.CAP_DSHOW)),
    ]


def test_list_macos_uses_injected_runner() -> None:
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout=_PROFILER_JSON)

    devs = _list_macos(run=run)
    assert [d.name for d in devs] == ["FaceTime HD Camera", "See3CAM_24CUG"]
    assert all(d.backend == int(cv2.CAP_AVFOUNDATION) for d in devs)
    assert calls[0][0] == "system_profiler"


def test_list_devices_failure_returns_empty_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="system_profiler", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", boom)
    with caplog.at_level("WARNING"):
        assert list_devices(platform="darwin") == []
    assert any("fallback_index" in r.message for r in caplog.records)
    with caplog.at_level("WARNING"):
        assert list_devices(platform="linux") == []


def test_windows_enumeration_via_pygrabber(monkeypatch: pytest.MonkeyPatch) -> None:
    """The win32 branch pairs pygrabber's DirectShow filter order with
    CAP_DSHOW indexes (exercised via a stub module — pygrabber itself is
    win32-only and never installed on dev machines)."""
    import sys
    import types

    graph_mod = types.ModuleType("pygrabber.dshow_graph")

    class FilterGraph:
        def get_input_devices(self) -> list[str]:
            return ["Integrated Webcam", "See3CAM_24CUG"]

    graph_mod.FilterGraph = FilterGraph  # type: ignore[attr-defined]
    pkg = types.ModuleType("pygrabber")
    pkg.dshow_graph = graph_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pygrabber", pkg)
    monkeypatch.setitem(sys.modules, "pygrabber.dshow_graph", graph_mod)

    devs = list_devices(platform="win32")
    assert [(d.name, d.index) for d in devs] == [
        ("Integrated Webcam", 0),
        ("See3CAM_24CUG", 1),
    ]
    assert all(d.backend == int(cv2.CAP_DSHOW) for d in devs)


def test_windows_enumeration_failure_returns_empty_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import sys

    monkeypatch.delitem(sys.modules, "pygrabber", raising=False)
    monkeypatch.delitem(sys.modules, "pygrabber.dshow_graph", raising=False)
    with caplog.at_level("WARNING"):
        assert list_devices(platform="win32") == []  # import fails on macOS
    assert any("fallback_index" in r.message for r in caplog.records)


# -- find_device ---------------------------------------------------------------


def test_find_device_case_insensitive_substring() -> None:
    devs = _devs("See3CAM_24CUG", "See3CAM_37CUGM", "FaceTime HD Camera")
    assert find_device(devs, "see3cam_24cug").index == 0
    assert find_device(devs, "37CUGM").index == 1


def test_find_device_ambiguous_raises_listing_matches() -> None:
    devs = _devs("See3CAM_24CUG", "See3CAM_37CUGM")
    with pytest.raises(ValueError, match="ambiguous.*24CUG.*37CUGM"):
        find_device(devs, "See3CAM")


def test_find_device_no_match_lists_enumerated() -> None:
    with pytest.raises(ValueError, match="no camera matching.*FaceTime"):
        find_device(_devs("FaceTime HD Camera"), "See3CAM_24CUG")


# -- backend_flag ----------------------------------------------------------------


def test_backend_flag_mapping() -> None:
    assert backend_flag(Backend.DSHOW) == int(cv2.CAP_DSHOW)
    assert backend_flag(Backend.MSMF) == int(cv2.CAP_MSMF)
    assert backend_flag(Backend.AVFOUNDATION) == int(cv2.CAP_AVFOUNDATION)
    assert backend_flag(Backend.AUTO, platform="win32") == int(cv2.CAP_DSHOW)
    assert backend_flag(Backend.AUTO, platform="darwin") == int(
        cv2.CAP_AVFOUNDATION
    )
    assert backend_flag(Backend.AUTO, platform="linux") == int(cv2.CAP_ANY)


# -- fake self-checks (the harness later steps lean on) -------------------------


def test_fake_capture_scripts_hooks_and_recording() -> None:
    cap = FakeCapture(hooks=dshow_hooks(), read_script=["ok", "fail"])
    assert cap.set(cv2.CAP_PROP_EXPOSURE, -5.7)  # quantized to a whole stop
    assert cap.get(cv2.CAP_PROP_EXPOSURE) == -6.0
    assert not cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.33)  # rejected value
    ok, frame = cap.read()
    assert ok and isinstance(frame, np.ndarray)
    ok, frame = cap.read()
    assert not ok and frame is None
    assert cap.read()[0]  # script exhausted -> after="ok"
    assert cap.sets_for(cv2.CAP_PROP_EXPOSURE) == [-5.7]
    cap.release()
    assert not cap.isOpened()


def test_fake_factory_records_and_scripts() -> None:
    factory = FakeCaptureFactory(captures=[FakeCapture(opened=False)])
    first = factory(2, int(cv2.CAP_DSHOW))
    second = factory(3, int(cv2.CAP_MSMF))
    assert not first.isOpened() and second.isOpened()
    assert factory.calls == [(2, int(cv2.CAP_DSHOW)), (3, int(cv2.CAP_MSMF))]
    assert factory.created == [first, second]
