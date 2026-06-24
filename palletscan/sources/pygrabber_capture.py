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
) -> int | None:
    """Pick a get_formats() entry index: Y8 at the target res, then any Y8,
    then any entry at the target res. Pure (no cv2) so it is unit-testable."""

    def is_y8(f: dict) -> bool:
        return str(f.get("media_type_str", "")).strip() in _Y8_NAMES

    def is_res(f: dict) -> bool:
        return f.get("width") == width and f.get("height") == height

    order = []
    if prefer_y8 and width and height:
        order.append(lambda f: is_y8(f) and is_res(f))
    if prefer_y8:
        order.append(is_y8)
    if width and height:
        order.append(is_res)
    for pred in order:
        hit = next((f for f in formats if pred(f)), None)
        if hit is not None:
            return hit.get("index")
    return None


class PyGrabberCaptureError(RuntimeError):
    """Graph build/format selection failed; the capture reports not-opened."""


class PyGrabberCapture:
    """``Capture``-protocol wrapper over a pygrabber DirectShow SampleGrabber,
    with the whole graph owned by one COM-initialized worker thread."""

    def __init__(
        self,
        index: int,
        *,
        width: int | None = None,
        height: int | None = None,
        prefer_y8: bool = True,
        open_timeout_s: float = 4.0,
        read_timeout_s: float = 0.5,
    ) -> None:
        self._index = index
        self._req_w = width
        self._req_h = height
        self._prefer_y8 = prefer_y8
        self._open_timeout = open_timeout_s
        self._read_timeout = read_timeout_s

        # negotiated geometry (filled by the owner thread before first frame)
        self._w = width
        self._h = height
        self._fourcc_name = "Y8  "
        self._mono = True  # mono backend by purpose; refined from the negotiated fourcc
        self._fps_echo = float(0)

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
        if not self._ready.wait(self._open_timeout + 8.0):
            log.error("PyGrabberCapture[%d]: open timed out waiting for owner thread", index)
            self._stop.set()
            self._opened = False

    # -- owner thread: ALL DirectShow/COM lives here ----------------------
    def _run(self) -> None:
        patch_pygrabber_subtypes()
        com_init = False
        graph = None
        try:
            try:
                comtypes.CoInitialize()
                com_init = True
            except OSError:
                pass  # already initialized on this thread (tolerable)
            from pygrabber.dshow_graph import FilterGraph, FilterType

            graph = FilterGraph()
            graph.add_video_input_device(self._index)
            dev = graph.get_input_device()
            self._select_format(dev)  # raises PyGrabberCaptureError on no match
            self._cam_ctrl, self._proc_amp = dshow_controls.acquire(dev.instance)
            self._has_controls = bool(self._cam_ctrl or self._proc_amp)
            graph.add_sample_grabber(self._on_frame)
            graph.add_null_render()
            graph.prepare_preview_graph()
            graph.run()
            self._grab_cb = graph.filters[FilterType.sample_grabber].callback
            self._grab_cb.keep_photo = True  # arm the first frame (callback re-arms)

            # Liveness gate: only "open" if a real frame actually arrives.
            with self._cond:
                got = self._cond.wait_for(lambda: self._seq > 0, self._open_timeout)
            self._opened = bool(got)
            if not got:
                self._build_error = "no frame within open timeout (dead graph)"
                log.error("PyGrabberCapture[%d]: %s", self._index, self._build_error)
            else:
                log.info("PyGrabberCapture[%d]: streaming %s %sx%s via DirectShow",
                         self._index, self._fourcc_name, self._w, self._h)
            self._ready.set()
            if got:
                self._command_loop()  # serve control set()s on this COM thread; frames flow via callback
        except Exception as exc:  # build/run failure
            self._build_error = repr(exc)
            self._opened = False
            log.exception("PyGrabberCapture[%d]: graph build/run failed", self._index)
            self._ready.set()
        finally:
            self._teardown(graph)
            if com_init:
                try:
                    comtypes.CoUninitialize()
                except OSError:
                    pass

    def _select_format(self, dev) -> None:
        formats = dev.get_formats()  # may raise -> caught as build failure
        idx = choose_format(formats, self._req_w, self._req_h, prefer_y8=self._prefer_y8)
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
            self._ae_auto = value >= 0.5
            if self._cam_ctrl is not None:
                try:
                    cur, _f = self._cam_ctrl.Get(dshow_controls.CameraControl_Exposure)
                    dshow_controls.set_exposure(self._cam_ctrl, cur, auto=self._ae_auto)
                except Exception:
                    pass
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
            return float(self._fps_echo)
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
            self._fps_echo = value
            return True
        if prop in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
                    cv2.CAP_PROP_FOURCC, cv2.CAP_PROP_CONVERT_RGB):
            return True
        return False

    def release(self) -> None:
        self._opened = False
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
