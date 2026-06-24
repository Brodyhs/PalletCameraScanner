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
    IdentityInfo,
    backend_flag,
    devices_from_monikers,
    devices_from_names,
    find_device,
    identity_for_name,
    list_devices,
    parse_system_profiler,
    parse_vid_pid,
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


def test_windows_enumeration_reads_friendly_name_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The win32 branch pairs DirectShow moniker order with CAP_DSHOW
    indexes AND reads FriendlyName + DevicePath off each moniker's property
    bag (the COM enumeration is faked — it is win32-only and machine-
    specific). The identity (vid/pid parsed from DevicePath) is what the
    MSMF identity guard later fingerprints."""
    import palletscan.sources.devices as dev_mod

    # Two fake monikers carrying name + DevicePath; the second has the
    # real-shaped USB path so vid/pid parse out.
    monikers = [
        {
            "FriendlyName": "Integrated Webcam",
            "DevicePath": r"\\?\usb#vid_0c45&pid_6366&mi_00#abc#{guid}\global",
        },
        {
            "FriendlyName": "See3CAM_24CUG",
            "DevicePath": r"\\?\usb#vid_2560&pid_c128&mi_00#def#{guid}\global",
        },
    ]
    monkeypatch.setattr(
        dev_mod, "_enumerate_windows_monikers", lambda: monikers
    )
    monkeypatch.setattr(
        dev_mod, "_read_moniker_prop", lambda m, prop: m[prop]
    )

    devs = list_devices(platform="win32")
    assert [(d.name, d.index) for d in devs] == [
        ("Integrated Webcam", 0),
        ("See3CAM_24CUG", 1),
    ]
    assert all(d.backend == int(cv2.CAP_DSHOW) for d in devs)
    color = devs[1]
    assert color.identity is not None
    assert color.identity.friendly_name == "See3CAM_24CUG"
    assert (color.identity.vid, color.identity.pid) == ("2560", "c128")
    assert "vid_2560" in color.identity.device_path


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


# -- identity: parse_vid_pid ---------------------------------------------------


def test_parse_vid_pid_over_real_shaped_device_path() -> None:
    # A real See3CAM_24CUG DevicePath (the live box reads exactly this).
    path = (
        r"\\?\usb#vid_2560&pid_c128&mi_00#7&1250ea3a&0&0000#"
        r"{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
    )
    assert parse_vid_pid(path) == ("2560", "c128")  # lowercased hex


def test_parse_vid_pid_uppercase_normalized_to_lower() -> None:
    assert parse_vid_pid(r"USB#VID_2560&PID_C128#x") == ("2560", "c128")


def test_parse_vid_pid_no_vid_fallback_and_none_path() -> None:
    # No VID in the path -> both None (the no-VID fallback).
    assert parse_vid_pid(r"\\?\some#non_usb#path") == (None, None)
    assert parse_vid_pid(None) == (None, None)
    assert parse_vid_pid("") == (None, None)
    # VID present but PID absent (composite device): VID still parses.
    assert parse_vid_pid("usb#vid_2560&mi_00") == ("2560", None)


# -- identity: devices_from_monikers ------------------------------------------


def test_devices_from_monikers_reads_name_path_and_identity() -> None:
    monikers = [
        {
            "FriendlyName": "Integrated Webcam",
            "DevicePath": r"usb#vid_0c45&pid_6366&mi_00#x",
        }
    ]
    devs = devices_from_monikers(
        monikers, int(cv2.CAP_DSHOW), read_str=lambda m, p: m[p]
    )
    assert devs[0].name == "Integrated Webcam"
    assert devs[0].index == 0
    assert devs[0].identity == IdentityInfo(
        friendly_name="Integrated Webcam",
        device_path=r"usb#vid_0c45&pid_6366&mi_00#x",
        vid="0c45",
        pid="6366",
    )


def test_devices_from_monikers_device_path_absent_degrades_to_none() -> None:
    """A virtual/composite device may have a FriendlyName but no readable
    DevicePath: the read is wrapped, identity keeps the name, path/vid/pid
    are None — enumerable, just not VID/PID-fingerprintable."""

    def read_str(m, prop):
        if prop == "DevicePath":
            raise OSError("no DevicePath on this moniker")
        return m[prop]

    monikers = [{"FriendlyName": "OBS Virtual Camera"}]
    devs = devices_from_monikers(monikers, int(cv2.CAP_DSHOW), read_str=read_str)
    assert devs[0].identity == IdentityInfo(
        friendly_name="OBS Virtual Camera",
        device_path=None,
        vid=None,
        pid=None,
    )


def test_devices_from_names_has_no_identity() -> None:
    # The name-only path (macOS + every fake) leaves identity None so the
    # guard treats those devices as unverifiable, never as a mismatch.
    devs = devices_from_names(["See3CAM_24CUG"], int(cv2.CAP_DSHOW))
    assert devs[0].identity is None


def test_identity_for_name_resolves_unique_match() -> None:
    color = DeviceInfo(
        "See3CAM_24CUG",
        0,
        int(cv2.CAP_DSHOW),
        IdentityInfo("See3CAM_24CUG", "usb#vid_2560&pid_c128", "2560", "c128"),
    )
    other = DeviceInfo("Integrated Webcam", 1, int(cv2.CAP_DSHOW))
    assert identity_for_name([color, other], "24cug") == color.identity
    # Name-only device -> None identity.
    assert identity_for_name([color, other], "Integrated") is None
    # Absent / ambiguous -> None (find_device already surfaces those).
    assert identity_for_name([color, other], "nope") is None


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
