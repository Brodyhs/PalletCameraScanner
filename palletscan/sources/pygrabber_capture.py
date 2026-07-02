"""DirectShow SampleGrabber capture for mono (Y8/Y12) cameras OpenCV can't read.

Why this exists: the See3CAM_37CUGM (Sony IMX900 mono) exposes ONLY Y8/Y12
monochrome formats. OpenCV's Windows backends both fail on it — MSMF negotiates
Y8 but its source reader never starts (0 frames, MF error -1072875852); DSHOW
won't even open the device. e-CAMView streams it fine via a DirectShow
**SampleGrabber** graph, and ``pygrabber`` — already a dependency — drives that
same graph. This wraps it behind the ``Capture`` protocol the pipeline already
speaks (``isOpened``/``read``/``get``/``set``/``release``), so ``CameraSource``'s
watchdog / reconnect / mode / settings machinery runs unchanged.

InfoSec: pygrabber is an already-installed pip package; no new dependency, no
vendor SDK, no driver. The camera streams over the native Windows UVC driver.

THREADING / COM (the load-bearing design): comtypes auto-initializes COM only on
the thread that first imports it. The pipeline builds/reopens/closes a camera on
several different threads (construction thread, watchdog consumer thread), and
DirectShow graph objects are apartment-bound to the thread that created them.
Driving a graph from a thread that never called ``CoInitialize`` is undefined
(crash/hang). So this class **owns its entire DirectShow graph on one dedicated
thread** that ``CoInitialize``s on entry and ``CoUninitialize``s on exit: build,
run, frame delivery, and teardown all happen there. The public methods touch only
plain Python state (a lock-guarded latest frame + a few Events), never COM — so
they are safe to call from any thread.

Delivery model: the graph runs continuously. pygrabber's BufferCB is one-shot
(it clears ``keep_photo`` after each frame), so the frame callback re-arms it,
giving a steady stream. ``read()`` blocks for the NEXT frame (a sequence counter
under a Condition), matching ``cv2.VideoCapture`` semantics — no stale frames, and
``measure_achieved_fps`` reads the real rate (~72 fps at Y8 2064x1552).

Known constraint — RGB24 SampleGrabber: pygrabber hardcodes the grabber media
type to RGB24 and its ``SampleGrabberCallback.BufferCB`` reshapes the buffer as
``(h, w, 3)``, so a mono Y8 stream is colour-converted by an inserted DirectShow
filter and full-copied once inside pygrabber (~9.6 MB/frame at 2064x1552) before
``_on_frame`` slices one luma channel (one further ~3.2 MB copy — the only
Python-side copy; cv2 needs a contiguous array). Grabbing Y800/GREY directly
would require replacing BufferCB (pygrabber's is 3-channel-only), i.e. forking
pygrabber internals; the measured ~63-72 fps at full res does not justify that
fork today.

Frame-rate honesty: pygrabber's ``set_format`` programs the capability's OWN
media type and exposes no ``AvgTimePerFrame`` hook, so this backend can only
*select* a capability — it cannot program an arbitrary rate. ``set(CAP_PROP_FPS)``
therefore succeeds only when the negotiated capability is fixed-rate at the
request; otherwise it returns False so the control report files fps as rejected
instead of fabricated-verified. ``get(CAP_PROP_FPS)`` returns the known
device-programmed rate, or 0.0 when the capability's default interval is unknown.
"""

from __future__ import annotations

import logging
import queue
import threading

import comtypes
import cv2
import numpy as np

from palletscan.sources import dshow_controls

log = logging.getLogger(__name__)

#: UVC control props routed to the owner thread's COM apartment + cached for get().
_CONTROL_PROPS = frozenset({
    cv2.CAP_PROP_EXPOSURE,
    cv2.CAP_PROP_GAIN,
    cv2.CAP_PROP_AUTO_EXPOSURE,
    cv2.CAP_PROP_BRIGHTNESS,
})

#: FourCC -> media-subtype GUID is ``{<fourcc-le-hex>-0000-0010-8000-00AA00389B71}``.
#: pygrabber ships YUYV/UYVY/MJPG/... but NOT the mono codes, so get_formats()
#: KeyErrors on Y8/Y12. These are the ones this sensor family advertises.
_MONO_SUBTYPES: dict[str, str] = {
    "{20203859-0000-0010-8000-00AA00389B71}": "Y8  ",
    "{30303859-0000-0010-8000-00AA00389B71}": "Y800",
    "{20323159-0000-0010-8000-00AA00389B71}": "Y12 ",
    "{20363159-0000-0010-8000-00AA00389B71}": "Y16 ",
    "{59455247-0000-0010-8000-00AA00389B71}": "GREY",
}

#: media_type_str values we treat as 8-bit mono (preferred — OpenCV-grade luma).
_Y8_NAMES = frozenset({"Y8  ", "Y800", "GREY", "Y8", "L8"})

#: Negotiated (stripped) fourcc names that are monochrome. For ANY of these the
#: SampleGrabber's forced RGB24 output is replicated luma, so we can publish one
#: channel and skip a full-frame cvtColor. Distinct from _Y8_NAMES (which gates
#: format *preference*); this gates the single-channel *delivery* path.
_MONO_NAMES = frozenset({"Y8", "Y800", "Y12", "Y16", "GREY", "L8"})

#: Extra wait the constructor grants the owner thread on top of open_timeout_s
#: (graph build + liveness gate both happen there). Module-level so tests can
#: shrink it when exercising the constructor-timeout path.
_READY_GRACE_S = 8.0

#: A capability whose (normalized) framerate range spans no more than this is
#: fixed-rate: set_format programs its media type, so that rate IS the stream.
_FPS_FIXED_TOL = 0.01

#: Tolerance when matching a requested fps against a capability rate — covers
#: the 100 ns frame-interval rounding (e.g. 1e7/138889 = 71.99994 for "72").
_FPS_MATCH_TOL = 0.5


def _framerate_range(f: dict) -> tuple[float, float] | None:
    """Normalized (lo, hi) fps range of a get_formats() entry, or None.

    pygrabber computes ``min_framerate = 1e7 / MinFrameInterval`` — the SMALLEST
    interval, i.e. the HIGHEST fps — so its min/max keys arrive swapped; sort.
    """
    lo_hi = (f.get("min_framerate"), f.get("max_framerate"))
    if not all(isinstance(v, (int, float)) for v in lo_hi):
        return None
    lo, hi = sorted(float(v) for v in lo_hi)  # type: ignore[arg-type]
    return lo, hi


def patch_pygrabber_subtypes() -> None:
    """Teach pygrabber the mono media-subtype GUIDs (idempotent, in place).

    ``dshow_graph`` does ``from dshow_ids import subtypes``, binding the SAME
    dict object, so mutating ``dshow_ids.subtypes`` is visible there even after
    import. We only add missing keys (never rebind), so it is safe to call more
    than once and never clobbers pygrabber's own entries.
    """
    import pygrabber.dshow_ids as ids

    for guid, name in _MONO_SUBTYPES.items():
        ids.subtypes.setdefault(guid, name)


def choose_format(
    formats: list[dict],
    width: int | None,
    height: int | None,
    *,
    prefer_y8: bool = True,
    fps: float | None = None,
) -> int | None:
    """Pick a get_formats() entry index. Pure (no cv2) so it is unit-testable.

    Ranking: the EXACT configured resolution outranks pixel-format preference
    (a wrong-geometry frame trips CameraSource's shape gate -> infinite
    reconnect loop, so geometry is the hard constraint): Y8 at the target res,
    then any mono format at the target res (e.g. a Y12-only resolution), then
    anything at the target res, and only when the target res is absent
    entirely, any Y8. Within each tier a capability whose framerate range
    contains ``fps`` is preferred (falling back to tier order if none does).
    """

    def is_y8(f: dict) -> bool:
        return str(f.get("media_type_str", "")).strip() in _Y8_NAMES

    def is_mono(f: dict) -> bool:
        return str(f.get("media_type_str", "")).strip() in _MONO_NAMES

    def is_res(f: dict) -> bool:
        return f.get("width") == width and f.get("height") == height

    def fps_ok(f: dict) -> bool:
        if fps is None:
            return True
        rng = _framerate_range(f)
        if rng is None:
            return False  # rate unknown: never *preferred*, still reachable
        lo, hi = rng
        return lo - _FPS_MATCH_TOL <= fps <= hi + _FPS_MATCH_TOL

    order: list = []
    if width and height:
        if prefer_y8:
            order.append(lambda f: is_y8(f) and is_res(f))
            order.append(lambda f: is_mono(f) and is_res(f))
        order.append(is_res)
    if prefer_y8:
        order.append(is_y8)  # geometry fallback: target res offered by nothing
    for pred in order:
        hit = next((f for f in formats if pred(f) and fps_ok(f)), None)
        if hit is None:
            hit = next((f for f in formats if pred(f)), None)
        if hit is not None:
            return hit.get("index")
    return None


class PyGrabberCaptureError(RuntimeError):
    """Graph build/format selection failed; the capture reports not-opened."""


class _StopRequested(Exception):
    """release() (or a timed-out constructor) set _stop mid-build; bail out."""


class PyGrabberCapture:
    """``Capture``-protocol wrapper over a pygrabber DirectShow SampleGrabber,
    with the whole graph owned by one COM-initialized worker thread."""

    def __init__(
        self,
        index: int,
        *,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        prefer_y8: bool = True,
        open_timeout_s: float = 4.0,
        read_timeout_s: float = 0.5,
    ) -> None:
        self._index = index
        self._req_w = width
        self._req_h = height
        self._req_fps = fps
        self._prefer_y8 = prefer_y8
        self._open_timeout = open_timeout_s
        self._read_timeout = read_timeout_s

        # negotiated geometry (filled by the owner thread before first frame)
        self._w = width
        self._h = height
        self._fourcc_name = "Y8  "
        self._mono = True  # mono backend by purpose; refined from the negotiated fourcc
        # fps honesty: the capability's advertised range and, when the range is
        # fixed-rate, the rate set_format actually programmed. None == unknown.
        self._fps_range: tuple[float, float] | None = None
        self._fps_actual: float | None = None

        # cross-thread state (plain Python, no COM): latest frame + sequence
        self._cond = threading.Condition()
        self._latest: np.ndarray | None = None
        self._seq = 0
        self._last_read_seq = 0
        self._opened = False
        self._build_error: str | None = None

        self._ready = threading.Event()    # owner thread finished its open attempt
        self._stop = threading.Event()     # request owner thread to tear down
        self._grab_cb = None               # pygrabber SampleGrabberCallback (bool re-arm)

        # camera controls: acquired on the owner thread; set() marshals here.
        self._cmd_q: queue.Queue = queue.Queue()
        self._cam_ctrl = None
        self._proc_amp = None
        self._has_controls = False
        self._ae_auto = False
        self._ctl_readback: dict[int, float] = {}

        self._thread = threading.Thread(
            target=self._run, name=f"pygrabber-cam{index}", daemon=True
        )
        self._thread.start()
        # Block construction until the graph is up + first frame proven, or failed.
        if not self._ready.wait(self._open_timeout + _READY_GRACE_S):
            log.error("PyGrabberCapture[%d]: open timed out waiting for owner thread", index)
            # Under _cond so the abandoned owner thread's _opened transition
            # (also under _cond, gated on _stop) can never resurrect this
            # capture; it bails at its next _check_stop and tears down.
            with self._cond:
                self._stop.set()
                self._opened = False
                self._cond.notify_all()

    # -- owner thread: ALL DirectShow/COM lives here ----------------------
    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise _StopRequested()

    def _run(self) -> None:
        patch_pygrabber_subtypes()
        com_init = False
        graph = None
        dev = None
        try:
            try:
                comtypes.CoInitialize()
                com_init = True
            except OSError:
                pass  # already initialized on this thread (tolerable)
            from pygrabber.dshow_graph import FilterGraph, FilterType

            # A timed-out constructor (or an early release()) sets _stop while
            # we are still building: re-check between every COM step so an
            # abandoned thread bails out and NEVER calls graph.run() — that
            # would stream the EXCLUSIVE UVC device against later opens.
            self._check_stop()
            graph = FilterGraph()
            self._check_stop()
            graph.add_video_input_device(self._index)
            self._check_stop()
            dev = graph.get_input_device()
            self._select_format(dev)  # raises PyGrabberCaptureError on no match
            self._check_stop()
            self._cam_ctrl, self._proc_amp = dshow_controls.acquire(dev.instance)
            self._has_controls = bool(self._cam_ctrl or self._proc_amp)
            self._check_stop()
            graph.add_sample_grabber(self._on_frame)
            graph.add_null_render()
            self._check_stop()
            graph.prepare_preview_graph()
            self._check_stop()
            graph.run()
            self._grab_cb = graph.filters[FilterType.sample_grabber].callback
            self._grab_cb.keep_photo = True  # arm the first frame (callback re-arms)

            # Liveness gate: only "open" if a real frame actually arrives. The
            # transition is made under _cond and gated on _stop so it can never
            # overwrite a concurrent release()'s _opened=False (release() flips
            # both under the same lock).
            with self._cond:
                self._cond.wait_for(
                    lambda: self._seq > 0 or self._stop.is_set(), self._open_timeout
                )
                got = self._seq > 0 and not self._stop.is_set()
                self._opened = got
            if not got:
                self._build_error = (
                    "released during open" if self._stop.is_set()
                    else "no frame within open timeout (dead graph)"
                )
                log.error("PyGrabberCapture[%d]: %s", self._index, self._build_error)
            else:
                log.info("PyGrabberCapture[%d]: streaming %s %sx%s via DirectShow",
                         self._index, self._fourcc_name, self._w, self._h)
            self._ready.set()
            if got:
                self._command_loop()  # serve control set()s on this COM thread; frames flow via callback
        except _StopRequested:
            self._build_error = "released during graph build"
            self._opened = False
            self._ready.set()
        except Exception as exc:  # build/run failure
            self._build_error = repr(exc)
            self._opened = False
            log.exception("PyGrabberCapture[%d]: graph build/run failed", self._index)
            self._ready.set()
        finally:
            self._teardown(graph)
            # Drop the frame's COM interface refs (FilterGraph internals, the
            # device IBaseFilter) BEFORE CoUninitialize: comtypes Release()s
            # from __del__, and releasing an interface after its apartment is
            # uninitialized is undefined per COM rules.
            graph = None
            dev = None
            if com_init:
                try:
                    comtypes.CoUninitialize()
                except OSError:
                    pass

    def _select_format(self, dev) -> None:
        formats = dev.get_formats()  # may raise -> caught as build failure
        idx = choose_format(
            formats, self._req_w, self._req_h,
            prefer_y8=self._prefer_y8, fps=self._req_fps,
        )
        if idx is None:
            raise PyGrabberCaptureError(
                f"no Y8/target-res format among {len(formats)} for index {self._index}"
            )
        dev.set_format(idx)
        chosen = next((f for f in formats if f.get("index") == idx), {})
        self._w = chosen.get("width", self._req_w)
        self._h = chosen.get("height", self._req_h)
        self._fourcc_name = (str(chosen.get("media_type_str", "Y8  ")).strip() or "Y8") + ""
        self._mono = self._fourcc_name in _MONO_NAMES
        # set_format programs the capability's OWN media type: only a
        # fixed-rate capability tells us the streamed rate. A ranged
        # capability's default AvgTimePerFrame is not exposed by pygrabber,
        # so the actual rate stays unknown (get() reports 0.0 honestly).
        self._fps_range = _framerate_range(chosen)
        if self._fps_range is not None:
            lo, hi = self._fps_range
            if hi - lo <= _FPS_FIXED_TOL:
                self._fps_actual = hi

    def _teardown(self, graph) -> None:
        if graph is None:
            return
        try:
            graph.stop()
        except Exception:
            log.debug("PyGrabberCapture[%d]: stop() raised", self._index, exc_info=True)
        try:
            graph.remove_filters()
        except Exception:
            log.debug("PyGrabberCapture[%d]: remove_filters() raised", self._index,
                      exc_info=True)
        self._grab_cb = None
        self._cam_ctrl = None
        self._proc_amp = None

    # -- owner-thread control loop (COM objects are apartment-bound here) --
    def _command_loop(self) -> None:
        """Apply control set()s marshalled from any thread. Frame delivery is
        independent (the BufferCB self-re-arms), so blocking the queue is fine."""
        while not self._stop.is_set():
            try:
                prop, value, ev, res = self._cmd_q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                rb = self._apply_control(prop, value)
                res["ok"], res["readback"] = (rb is not None), rb
            except Exception as exc:  # never let a bad control wedge the thread
                res["ok"], res["readback"] = False, None
                log.warning("PyGrabberCapture[%d]: control %s=%s failed: %r",
                            self._index, prop, value, exc)
            ev.set()

    def _apply_control(self, prop: int, value: float) -> float | None:
        if prop == cv2.CAP_PROP_AUTO_EXPOSURE:
            # DSHOW-style sentinel: >=0.5 == auto. Re-assert exposure under the
            # new flag (using the current value) so manual/auto actually flips.
            # Honest failure: without a camera control, or when the driver
            # write fails, the flag never reached hardware — report None so
            # set() returns False (no fabricated sentinel readback).
            auto = value >= 0.5
            if self._cam_ctrl is None:
                return None
            try:
                cur, _f = self._cam_ctrl.Get(dshow_controls.CameraControl_Exposure)
            except Exception:
                log.warning(
                    "PyGrabberCapture[%d]: auto-exposure flag readback failed",
                    self._index, exc_info=True,
                )
                return None
            rb = dshow_controls.set_exposure(self._cam_ctrl, cur, auto=auto)
            if rb is None:
                return None
            self._ae_auto = auto  # only track a flag the driver confirmed
            return value  # echo the sentinel so apply_settings verifies the flag
        if prop == cv2.CAP_PROP_EXPOSURE:
            rb = dshow_controls.set_exposure(self._cam_ctrl, value, auto=self._ae_auto)
            return None if rb is None else float(rb)
        if prop == cv2.CAP_PROP_GAIN:
            rb = dshow_controls.set_proc_amp(
                self._proc_amp, dshow_controls.VideoProcAmp_Gain, value)
            return None if rb is None else float(rb)
        if prop == cv2.CAP_PROP_BRIGHTNESS:
            rb = dshow_controls.set_proc_amp(
                self._proc_amp, dshow_controls.VideoProcAmp_Brightness, value)
            return None if rb is None else float(rb)
        return None

    # -- DirectShow BufferCB (DShow streaming thread; no COM here) ---------
    def _on_frame(self, image: np.ndarray) -> None:
        # Re-arm the one-shot grabber FIRST so the next driver buffer is already
        # eligible while we publish this one (avoids a drop-every-other-frame
        # window — REVIEW CAM-05); skip once teardown has started (CAM-01).
        # keep_photo is the only off-thread COM-adjacent write, an atomic bool.
        if self._stop.is_set():
            return
        cb = self._grab_cb
        if cb is not None:
            cb.keep_photo = True  # re-arm the one-shot grabber -> continuous
        # The SampleGrabber forces RGB24, but a mono sensor's three channels are
        # replicated luma — publish ONE contiguous channel so the frame is 2-D at
        # ingest. CameraSource.to_gray then no-ops (skips a full-frame BGR2GRAY on
        # the reader thread) and everything downstream carries 1/3 the bytes. Gated
        # on the negotiated mono fourcc so a true-color cam on this backend is left
        # 3-channel for to_gray's cvtColor. ascontiguousarray (not a strided view)
        # keeps the array cv2-safe; it is cheaper than the cvtColor it replaces.
        if self._mono and image.ndim == 3:
            image = np.ascontiguousarray(image[:, :, 0])
        with self._cond:
            self._latest = image
            self._seq += 1
            self._cond.notify_all()

    # -- Capture protocol (any thread; touches no COM) --------------------
    def isOpened(self) -> bool:  # noqa: N802 - cv2 naming
        return self._opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._opened:
            return False, None
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > self._last_read_seq,
                                       self._read_timeout):
                return False, None
            self._last_read_seq = self._seq
            img = self._latest
        if img is None:
            return False, None
        return True, img

    def get(self, prop: int) -> float:  # noqa: N802
        if prop in _CONTROL_PROPS:
            return float(self._ctl_readback.get(prop, 0.0))
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._w or 0)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._h or 0)
        if prop == cv2.CAP_PROP_FPS:
            # Honest: the rate we KNOW the device was programmed to (a
            # fixed-rate capability), else 0.0 (cv2 "unknown") — never a
            # mirror of the caller's request.
            return float(self._fps_actual) if self._fps_actual is not None else 0.0
        if prop == cv2.CAP_PROP_FOURCC:
            # Honest: the real negotiated mono fourcc. We publish a single luma
            # channel in _on_frame for mono formats, so to_gray no-ops (2-D
            # passthrough); the value is informational for this backend.
            name = (self._fourcc_name or "Y8").ljust(4)[:4]
            try:
                return float(cv2.VideoWriter.fourcc(*name))
            except Exception:
                return 0.0
        if prop == cv2.CAP_PROP_CONVERT_RGB:
            return 1.0
        return 0.0

    def set(self, prop: int, value: float) -> bool:  # noqa: N802
        # UVC controls are marshalled to the owner thread's COM apartment and
        # applied synchronously (so the immediate get() in _set verifies).
        if prop in _CONTROL_PROPS:
            if not self._has_controls:
                return False
            ev = threading.Event()
            res: dict = {}
            self._cmd_q.put((prop, float(value), ev, res))
            if not ev.wait(2.0) or not res.get("ok"):
                return False
            self._ctl_readback[prop] = float(res["readback"])
            return True
        # Mode/format is fixed when the graph is built; accept the props the
        # connect path replays (so apply_mode stays warn-free) and ignore the rest.
        if prop == cv2.CAP_PROP_FPS:
            # The frame interval is NOT programmable through pygrabber's
            # set_format (no AvgTimePerFrame hook), so the request is honored
            # only when the negotiated capability is fixed-rate at (tolerably)
            # this value; otherwise reject so apply_mode files fps as rejected
            # instead of fabricated-verified (see module docstring).
            return (
                self._fps_actual is not None
                and abs(self._fps_actual - value) <= _FPS_MATCH_TOL
            )
        if prop in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
                    cv2.CAP_PROP_FOURCC, cv2.CAP_PROP_CONVERT_RGB):
            return True
        return False

    def release(self) -> None:
        # _stop and _opened flip together under _cond: the owner thread's
        # _opened transition takes the same lock and re-checks _stop, so a
        # release() can never be overwritten by a late "graph is live".
        with self._cond:
            self._stop.set()
            self._opened = False
            self._cond.notify_all()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
            if t.is_alive():
                # Unavoidable wedged-COM case: the owner thread is blocked
                # INSIDE a single COM/driver call (_check_stop only runs
                # between build steps). Abandon the daemon thread rather than
                # hang the caller; its graph is reclaimed only at process exit.
                log.warning(
                    "PyGrabberCapture[%d]: owner thread still alive 3s after "
                    "release (wedged COM call); abandoning daemon thread",
                    self._index,
                )
