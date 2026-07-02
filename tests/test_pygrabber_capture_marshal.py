"""Camera-free tests for PyGrabberCapture's pure marshaling/mapping logic.

We exercise the routing/cache methods WITHOUT starting the real owner thread:
``object.__new__`` builds a bare instance, then we inject just the plain-Python
state each method touches (fake controls, the readback cache, the command queue).
This keeps the test off COM and off DirectShow entirely while still running the
production ``_apply_control`` / ``set`` / ``get`` code.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import cv2

from palletscan.sources import dshow_controls
from palletscan.sources.pygrabber_capture import PyGrabberCapture
from tests.pygrabber_fakes import FakeCamCtrl, FakeProcAmp


def _bare_capture() -> Any:
    """A PyGrabberCapture with no owner thread and no live graph.

    ``__init__`` starts the DirectShow owner thread, so we bypass it with
    ``object.__new__`` and set only the plain-Python attributes the marshaling
    methods read/write. Nothing here touches COM. Typed ``Any`` so the test can
    inject fake control objects onto the ``None``-typed slots.
    """
    cap = object.__new__(PyGrabberCapture)
    cap._index = 7
    cap._cam_ctrl = None
    cap._proc_amp = None
    cap._has_controls = False
    cap._ae_auto = False
    cap._ctl_readback = {}
    cap._cmd_q = queue.Queue()
    cap._req_fps = None
    cap._fps_range = None
    cap._fps_actual = None
    cap._w = 2064
    cap._h = 1552
    cap._fourcc_name = "Y8  "
    return cap


# -- _apply_control routing --------------------------------------------------


def test_apply_control_exposure_routes_to_set_exposure() -> None:
    cap = _bare_capture()
    cap._cam_ctrl = FakeCamCtrl.with_exposure(mn=-13, mx=-1, default=-6)
    cap._ae_auto = False

    rb = cap._apply_control(cv2.CAP_PROP_EXPOSURE, -6.0)

    assert rb == -6.0
    prop, value, flags = cap._cam_ctrl.set_calls[-1]
    assert prop == dshow_controls.CameraControl_Exposure
    assert value == -6
    assert flags == dshow_controls.Flags_Manual


def test_apply_control_gain_routes_to_proc_amp() -> None:
    cap = _bare_capture()
    cap._proc_amp = FakeProcAmp.with_gain(mn=0, mx=100, default=0)

    rb = cap._apply_control(cv2.CAP_PROP_GAIN, 250.0)

    assert rb == 100.0  # clamped to max
    assert cap._proc_amp.set_calls[-1][0] == dshow_controls.VideoProcAmp_Gain


def test_apply_control_brightness_routes_to_proc_amp() -> None:
    cap = _bare_capture()
    cap._proc_amp = FakeProcAmp.with_gain()

    rb = cap._apply_control(cv2.CAP_PROP_BRIGHTNESS, 9.0)

    assert rb == 9.0
    assert cap._proc_amp.set_calls[-1][0] == dshow_controls.VideoProcAmp_Brightness


def test_apply_control_auto_exposure_flips_flag_and_reasserts() -> None:
    cap = _bare_capture()
    cap._cam_ctrl = FakeCamCtrl.with_exposure(default=-6)
    cap._ae_auto = False

    # Sentinel >= 0.5 -> auto: echoes the sentinel and re-asserts exposure under
    # the new flag (so manual/auto actually flips at the driver).
    rb = cap._apply_control(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0)
    assert rb == 1.0
    assert cap._ae_auto is True
    assert cap._cam_ctrl.set_calls[-1][2] == dshow_controls.Flags_Auto

    # Sentinel < 0.5 -> manual.
    rb = cap._apply_control(cv2.CAP_PROP_AUTO_EXPOSURE, 0.0)
    assert rb == 0.0
    assert cap._ae_auto is False
    assert cap._cam_ctrl.set_calls[-1][2] == dshow_controls.Flags_Manual


def test_apply_control_unknown_prop_returns_none() -> None:
    cap = _bare_capture()
    assert cap._apply_control(cv2.CAP_PROP_SATURATION, 5.0) is None


def test_apply_control_exposure_returns_none_when_no_camera() -> None:
    cap = _bare_capture()  # _cam_ctrl is None
    assert cap._apply_control(cv2.CAP_PROP_EXPOSURE, -6.0) is None


def test_apply_control_auto_exposure_returns_none_when_no_camera() -> None:
    """No IAMCameraControl -> the flag never reaches hardware, so no sentinel
    echo (REVIEW finding: the pre-fix code fabricated a successful readback)."""
    cap = _bare_capture()  # _cam_ctrl is None
    assert cap._apply_control(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0) is None
    assert cap._ae_auto is False  # unconfirmed flag is not tracked either


def test_apply_control_auto_exposure_returns_none_on_driver_error() -> None:
    class Boom(FakeCamCtrl):
        def Get(self, prop: int):  # noqa: N802 - COM naming
            raise OSError("device fell off the bus")

    cap = _bare_capture()
    cap._cam_ctrl = Boom.with_exposure()
    assert cap._apply_control(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0) is None
    assert cap._ae_auto is False


# -- set() control gate + command-queue marshaling ---------------------------


def test_set_returns_false_when_no_controls() -> None:
    cap = _bare_capture()
    cap._has_controls = False
    assert cap.set(cv2.CAP_PROP_EXPOSURE, -6.0) is False


def test_set_marshals_to_command_queue_and_caches_readback() -> None:
    """With controls present, set() enqueues a command and blocks on the event.
    We drain the queue from a worker thread (standing in for _command_loop) and
    resolve it, then assert the readback was cached for get()."""
    cap = _bare_capture()
    cap._has_controls = True

    def worker() -> None:
        prop, value, ev, res = cap._cmd_q.get(timeout=2.0)
        # Mimic _command_loop resolving the command.
        res["ok"], res["readback"] = True, -6.0
        ev.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    assert cap.set(cv2.CAP_PROP_EXPOSURE, -6.0) is True
    t.join(timeout=2.0)
    assert cap._ctl_readback[cv2.CAP_PROP_EXPOSURE] == -6.0
    assert cap.get(cv2.CAP_PROP_EXPOSURE) == -6.0


def test_set_returns_false_when_command_reports_not_ok() -> None:
    cap = _bare_capture()
    cap._has_controls = True

    def worker() -> None:
        _prop, _value, ev, res = cap._cmd_q.get(timeout=2.0)
        res["ok"], res["readback"] = False, None
        ev.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert cap.set(cv2.CAP_PROP_GAIN, 10.0) is False
    t.join(timeout=2.0)


def test_set_format_props_accepted_without_controls() -> None:
    cap = _bare_capture()
    # fps honesty (REVIEW finding 8): the frame interval is never programmable
    # after the graph is built, so set(FPS) succeeds ONLY when the negotiated
    # capability is fixed-rate at the request — never an accepted mirror. (The
    # old assertion here pinned the fabricated echo and was itself the bug.)
    assert cap.set(cv2.CAP_PROP_FPS, 60.0) is False
    cap._fps_actual = 60.0
    assert cap.set(cv2.CAP_PROP_FPS, 60.0) is True
    assert cap.set(cv2.CAP_PROP_FPS, 30.0) is False
    # Geometry props the connect path replays are accepted no-ops.
    assert cap.set(cv2.CAP_PROP_FRAME_WIDTH, 2064.0) is True
    assert cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1552.0) is True
    assert cap.set(cv2.CAP_PROP_FOURCC, 0.0) is True
    assert cap.set(cv2.CAP_PROP_CONVERT_RGB, 1.0) is True
    # An unhandled prop is rejected.
    assert cap.set(cv2.CAP_PROP_SATURATION, 1.0) is False


# -- get() mapping + readback cache ------------------------------------------


def test_get_geometry_and_fps() -> None:
    cap = _bare_capture()
    assert cap.get(cv2.CAP_PROP_FRAME_WIDTH) == 2064.0
    assert cap.get(cv2.CAP_PROP_FRAME_HEIGHT) == 1552.0
    # fps: 0.0 (cv2 "unknown") until a device-programmed rate is known —
    # never a mirror of a caller's request (REVIEW finding 8).
    assert cap.get(cv2.CAP_PROP_FPS) == 0.0
    cap._fps_actual = 72.0
    assert cap.get(cv2.CAP_PROP_FPS) == 72.0


def test_get_fourcc_encodes_negotiated_name() -> None:
    cap = _bare_capture()
    cap._fourcc_name = "Y8  "
    assert cap.get(cv2.CAP_PROP_FOURCC) == float(cv2.VideoWriter.fourcc(*"Y8  "))


def test_get_convert_rgb_is_one() -> None:
    cap = _bare_capture()
    assert cap.get(cv2.CAP_PROP_CONVERT_RGB) == 1.0


def test_get_control_props_read_cache_default_zero() -> None:
    cap = _bare_capture()
    # Uncached control prop -> 0.0 (cv2 "unsupported").
    assert cap.get(cv2.CAP_PROP_GAIN) == 0.0
    cap._ctl_readback[cv2.CAP_PROP_GAIN] = 33.0
    assert cap.get(cv2.CAP_PROP_GAIN) == 33.0


def test_get_unknown_prop_returns_zero() -> None:
    cap = _bare_capture()
    assert cap.get(cv2.CAP_PROP_SATURATION) == 0.0
