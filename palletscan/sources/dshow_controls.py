"""Raw comtypes declarations for DirectShow IAMCameraControl / IAMVideoProcAmp.

pygrabber exposes no camera-control API, so to make the pygrabber mono's
exposure/gain settable from the pipeline we declare the two standard DirectShow
control interfaces directly and QueryInterface them off the video-input filter.
Mirrors pygrabber's own COMMETHOD idiom (.venv/.../pygrabber/dshow_core.py).

All access MUST happen on the COM apartment thread that built the graph
(PyGrabberCapture's owner thread) — these objects are apartment-bound.

Interfaces (DirectShow, ksmedia.h / strmif.h):
  IAMCameraControl {C6E13370-30AC-11D0-A18C-00A0C9118956}
  IAMVideoProcAmp  {C6E13360-30AC-11D0-A18C-00A0C9118956}
Each: GetRange(prop)->(min,max,step,default,caps); Set(prop,value,flags);
Get(prop)->(value,flags). Exposure is log2(seconds) (DirectShow convention:
value -6 == 2**-6 s ~= 15.6 ms), so it maps 1:1 to our raw exposure config.
"""

from __future__ import annotations

import logging
from ctypes import HRESULT, POINTER, c_long

from comtypes import COMMETHOD, GUID, IUnknown

log = logging.getLogger(__name__)

# -- property selectors -------------------------------------------------------
CameraControl_Exposure = 4
VideoProcAmp_Brightness = 0
VideoProcAmp_Gain = 9

# -- flags (same values for both interfaces) ----------------------------------
Flags_Auto = 0x0001
Flags_Manual = 0x0002


class IAMCameraControl(IUnknown):
    _iid_ = GUID("{C6E13370-30AC-11D0-A18C-00A0C9118956}")


IAMCameraControl._methods_ = [
    COMMETHOD([], HRESULT, "GetRange",
              (["in"], c_long, "Property"),
              (["out"], POINTER(c_long), "pMin"),
              (["out"], POINTER(c_long), "pMax"),
              (["out"], POINTER(c_long), "pSteppingDelta"),
              (["out"], POINTER(c_long), "pDefault"),
              (["out"], POINTER(c_long), "pCapsFlags")),
    COMMETHOD([], HRESULT, "Set",
              (["in"], c_long, "Property"),
              (["in"], c_long, "lValue"),
              (["in"], c_long, "Flags")),
    COMMETHOD([], HRESULT, "Get",
              (["in"], c_long, "Property"),
              (["out"], POINTER(c_long), "lValue"),
              (["out"], POINTER(c_long), "Flags")),
]


class IAMVideoProcAmp(IUnknown):
    _iid_ = GUID("{C6E13360-30AC-11D0-A18C-00A0C9118956}")


IAMVideoProcAmp._methods_ = [
    COMMETHOD([], HRESULT, "GetRange",
              (["in"], c_long, "Property"),
              (["out"], POINTER(c_long), "pMin"),
              (["out"], POINTER(c_long), "pMax"),
              (["out"], POINTER(c_long), "pSteppingDelta"),
              (["out"], POINTER(c_long), "pDefault"),
              (["out"], POINTER(c_long), "pCapsFlags")),
    COMMETHOD([], HRESULT, "Set",
              (["in"], c_long, "Property"),
              (["in"], c_long, "lValue"),
              (["in"], c_long, "Flags")),
    COMMETHOD([], HRESULT, "Get",
              (["in"], c_long, "Property"),
              (["out"], POINTER(c_long), "lValue"),
              (["out"], POINTER(c_long), "Flags")),
]


def acquire(filter_instance):
    """QueryInterface IAMCameraControl + IAMVideoProcAmp off a DirectShow video
    input filter (pygrabber ``VideoInput.instance``). Returns (cam_ctrl, proc_amp);
    either may be None if the device doesn't expose it. Call on the owner thread."""
    cam_ctrl = proc_amp = None
    try:
        cam_ctrl = filter_instance.QueryInterface(IAMCameraControl)
    except Exception:
        log.info("dshow_controls: device has no IAMCameraControl (no exposure control)")
    try:
        proc_amp = filter_instance.QueryInterface(IAMVideoProcAmp)
    except Exception:
        log.info("dshow_controls: device has no IAMVideoProcAmp (no gain/brightness)")
    return cam_ctrl, proc_amp


def _clamp(value: int, rng: tuple[int, int, int]) -> int:
    mn, mx, step = rng
    v = max(mn, min(mx, int(value)))
    if step > 1:
        v = mn + round((v - mn) / step) * step
        v = max(mn, min(mx, v))
    return v


def set_exposure(cam_ctrl, value: float, *, auto: bool) -> int | None:
    """Set exposure (log2 seconds) with the auto/manual flag. Returns the
    readback value, or None if unsupported/failed."""
    if cam_ctrl is None:
        return None
    flag = Flags_Auto if auto else Flags_Manual
    try:
        mn, mx, step, _dflt, _caps = cam_ctrl.GetRange(CameraControl_Exposure)
        v = _clamp(value, (mn, mx, step))
        cam_ctrl.Set(CameraControl_Exposure, v, flag)
        rb, _f = cam_ctrl.Get(CameraControl_Exposure)
        return int(rb)
    except Exception:
        log.warning("dshow_controls: set_exposure(%s, auto=%s) failed", value, auto,
                    exc_info=True)
        return None


def set_proc_amp(proc_amp, prop: int, value: float) -> int | None:
    """Set a VideoProcAmp property (gain/brightness) in manual mode; returns
    the readback, or None if unsupported/failed."""
    if proc_amp is None:
        return None
    try:
        mn, mx, step, _dflt, _caps = proc_amp.GetRange(prop)
        v = _clamp(value, (mn, mx, step))
        proc_amp.Set(prop, v, Flags_Manual)
        rb, _f = proc_amp.Get(prop)
        return int(rb)
    except Exception:
        log.warning("dshow_controls: set_proc_amp(%s, %s) failed", prop, value,
                    exc_info=True)
        return None
