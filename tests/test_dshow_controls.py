"""Camera-free unit tests for the DirectShow IAMCameraControl / IAMVideoProcAmp
wrappers (``palletscan.sources.dshow_controls``).

The COM object is just a plain argument to ``set_exposure`` / ``set_proc_amp`` /
``acquire``, so these tests need NO ``sys.modules`` patching and NO real COM —
a :class:`FakeCamCtrl` / :class:`FakeProcAmp` exposing ``GetRange``/``Set``/``Get``
is enough to lock down clamping, the auto/manual flag, and the missing-interface
fallbacks.
"""

from __future__ import annotations

from palletscan.sources import dshow_controls
from palletscan.sources.dshow_controls import (
    CameraControl_Exposure,
    Flags_Auto,
    Flags_Manual,
    VideoProcAmp_Brightness,
    VideoProcAmp_Gain,
)
from tests.pygrabber_fakes import FakeCamCtrl, FakeProcAmp, FakeRangeProp


# -- set_exposure ------------------------------------------------------------


def test_set_exposure_clamps_low_to_range_min() -> None:
    cam = FakeCamCtrl.with_exposure(mn=-13, mx=-1, default=-6)
    rb = dshow_controls.set_exposure(cam, -50.0, auto=False)
    assert rb == -13  # clamped up to the GetRange min
    assert cam.set_calls[-1][:2] == (CameraControl_Exposure, -13)


def test_set_exposure_clamps_high_to_range_max() -> None:
    cam = FakeCamCtrl.with_exposure(mn=-13, mx=-1, default=-6)
    rb = dshow_controls.set_exposure(cam, 99.0, auto=False)
    assert rb == -1  # clamped down to the GetRange max


def test_set_exposure_maps_log2_seconds_value_through_unchanged() -> None:
    # -6 == 2**-6 s ~= 15.6 ms; an in-range log2 value is set 1:1 (no rescale).
    cam = FakeCamCtrl.with_exposure(mn=-13, mx=-1, default=-1)
    rb = dshow_controls.set_exposure(cam, -6.0, auto=False)
    assert rb == -6
    assert cam.set_calls[-1][1] == -6


def test_set_exposure_quantizes_to_stepping_delta() -> None:
    cam = FakeCamCtrl(
        {CameraControl_Exposure: FakeRangeProp(mn=0, mx=100, step=10, default=0)}
    )
    rb = dshow_controls.set_exposure(cam, 47.0, auto=False)
    assert rb == 50  # snapped to the nearest multiple of the stepping delta


def test_set_exposure_manual_flag_set_and_read_back() -> None:
    cam = FakeCamCtrl.with_exposure()
    dshow_controls.set_exposure(cam, -6.0, auto=False)
    assert cam.set_calls[-1][2] == Flags_Manual
    _value, flag = cam.Get(CameraControl_Exposure)
    assert flag == Flags_Manual


def test_set_exposure_auto_flag_set_and_read_back() -> None:
    cam = FakeCamCtrl.with_exposure()
    dshow_controls.set_exposure(cam, -6.0, auto=True)
    assert cam.set_calls[-1][2] == Flags_Auto
    _value, flag = cam.Get(CameraControl_Exposure)
    assert flag == Flags_Auto


def test_set_exposure_none_camera_returns_none() -> None:
    assert dshow_controls.set_exposure(None, -6.0, auto=False) is None


def test_set_exposure_swallows_driver_error_returns_none() -> None:
    class Boom(FakeCamCtrl):
        def GetRange(self, prop: int):  # noqa: N802
            raise OSError("device fell off the bus")

    cam = Boom.with_exposure()
    assert dshow_controls.set_exposure(cam, -6.0, auto=False) is None


# -- set_proc_amp (gain / brightness) ----------------------------------------


def test_set_proc_amp_gain_clamps_and_reads_back() -> None:
    amp = FakeProcAmp.with_gain(mn=0, mx=100, default=0)
    rb = dshow_controls.set_proc_amp(amp, VideoProcAmp_Gain, 250.0)
    assert rb == 100  # clamped to GetRange max
    value, flag = amp.Get(VideoProcAmp_Gain)
    assert value == 100
    assert flag == Flags_Manual  # proc-amp is always set manual


def test_set_proc_amp_gain_in_range_passes_through() -> None:
    amp = FakeProcAmp.with_gain(mn=0, mx=100, default=0)
    rb = dshow_controls.set_proc_amp(amp, VideoProcAmp_Gain, 42.0)
    assert rb == 42


def test_set_proc_amp_brightness_routes_to_its_property() -> None:
    amp = FakeProcAmp.with_gain()
    rb = dshow_controls.set_proc_amp(amp, VideoProcAmp_Brightness, 9.0)
    assert rb == 9
    assert amp.set_calls[-1][0] == VideoProcAmp_Brightness


def test_set_proc_amp_none_returns_none() -> None:
    assert dshow_controls.set_proc_amp(None, VideoProcAmp_Gain, 10.0) is None


def test_set_proc_amp_swallows_driver_error_returns_none() -> None:
    class Boom(FakeProcAmp):
        def Set(self, prop: int, value: int, flags: int) -> None:  # noqa: N802
            raise OSError("set rejected")

    amp = Boom.with_gain()
    assert dshow_controls.set_proc_amp(amp, VideoProcAmp_Gain, 10.0) is None


# -- acquire() interface-missing fallbacks -----------------------------------


def test_acquire_returns_both_interfaces_when_present() -> None:
    cam = FakeCamCtrl.with_exposure()
    amp = FakeProcAmp.with_gain()

    class Filter:
        def QueryInterface(self, iface):  # noqa: N802 - COM naming
            if iface is dshow_controls.IAMCameraControl:
                return cam
            if iface is dshow_controls.IAMVideoProcAmp:
                return amp
            raise OSError("no such interface")

    got_cam, got_amp = dshow_controls.acquire(Filter())
    assert got_cam is cam
    assert got_amp is amp


def test_acquire_falls_back_to_none_when_camera_control_missing() -> None:
    amp = FakeProcAmp.with_gain()

    class Filter:
        def QueryInterface(self, iface):  # noqa: N802
            if iface is dshow_controls.IAMVideoProcAmp:
                return amp
            raise OSError("E_NOINTERFACE")

    got_cam, got_amp = dshow_controls.acquire(Filter())
    assert got_cam is None  # graceful: missing interface -> None, no raise
    assert got_amp is amp


def test_acquire_falls_back_to_none_when_proc_amp_missing() -> None:
    cam = FakeCamCtrl.with_exposure()

    class Filter:
        def QueryInterface(self, iface):  # noqa: N802
            if iface is dshow_controls.IAMCameraControl:
                return cam
            raise OSError("E_NOINTERFACE")

    got_cam, got_amp = dshow_controls.acquire(Filter())
    assert got_cam is cam
    assert got_amp is None


def test_acquire_returns_none_none_when_no_interfaces() -> None:
    class Filter:
        def QueryInterface(self, iface):  # noqa: N802
            raise OSError("E_NOINTERFACE")

    assert dshow_controls.acquire(Filter()) == (None, None)
