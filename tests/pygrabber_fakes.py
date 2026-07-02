"""Camera-free fakes for the pygrabber DirectShow mono-capture backend.

These stand in for the DirectShow COM objects so the PyGrabberCapture owner
thread (and the dshow_controls wrappers) can be exercised with NO real
hardware, NO real COM, and NO vendor SDK.

Three layers, mirroring the production seams:

* :class:`FakeCamCtrl` / :class:`FakeProcAmp` — plain objects exposing the
  ``GetRange``/``Set``/``Get`` tuple shape the comtypes ``IAMCameraControl`` /
  ``IAMVideoProcAmp`` wrappers call. ``dshow_controls`` takes the COM object as
  a plain argument, so these need no ``sys.modules`` patching.

* :class:`FakeFilterGraph` / :class:`FakeFilterType` / :class:`FakeSampleGrabber`
  — a scriptable ``pygrabber.dshow_graph`` graph. The graph drives the
  registered ``_on_frame`` callback on the *owner thread* (the way the real
  SampleGrabber BufferCB would), so the whole owner-thread lifecycle runs
  against fakes.

* :func:`fake_directshow` — a fixture that installs the fake ``pygrabber``
  modules into ``sys.modules`` and swaps a fake ``comtypes`` onto the
  ``pygrabber_capture`` module ATTRIBUTE (the owner thread resolves
  ``comtypes.CoInitialize`` through its module globals — a ``sys.modules``
  swap after import would be dead code) BEFORE PyGrabberCapture is built, and
  RESTORES both in a finalizer so CI can never hang on a leaked module or a
  live owner thread.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from enum import IntEnum

import numpy as np
import pytest


# -- camera-control COM fakes (dshow_controls) -------------------------------


class FakeRangeProp:
    """One controllable property: a GetRange tuple + current (value, flag)."""

    def __init__(
        self,
        *,
        mn: int,
        mx: int,
        step: int = 1,
        default: int = 0,
        caps: int = 0x0003,
        value: int | None = None,
        flag: int = 0x0002,
    ) -> None:
        self.range = (mn, mx, step, default, caps)
        self.value = default if value is None else value
        self.flag = flag


class _BaseControl:
    """Shared GetRange/Set/Get behaviour over a per-property store.

    Records every ``Set`` call (as ``(prop, value, flags)``) so tests can
    assert the flag the wrapper chose, and clamps the stored value to the
    declared range so Get() readback mirrors a real driver.
    """

    def __init__(self, props: dict[int, FakeRangeProp]) -> None:
        self._props = props
        self.set_calls: list[tuple[int, int, int]] = []

    def GetRange(self, prop: int):  # noqa: N802 - COM naming
        return self._props[prop].range  # KeyError -> "unsupported" path

    def Set(self, prop: int, value: int, flags: int) -> None:  # noqa: N802
        self.set_calls.append((prop, int(value), int(flags)))
        p = self._props[prop]
        mn, mx, _step, _dflt, _caps = p.range
        p.value = max(mn, min(mx, int(value)))
        p.flag = int(flags)

    def Get(self, prop: int):  # noqa: N802 - COM naming
        p = self._props[prop]
        return p.value, p.flag


class FakeCamCtrl(_BaseControl):
    """Stand-in for IAMCameraControl (exposure)."""

    @classmethod
    def with_exposure(
        cls,
        *,
        mn: int = -13,
        mx: int = -1,
        step: int = 1,
        default: int = -6,
    ) -> "FakeCamCtrl":
        from palletscan.sources import dshow_controls

        return cls(
            {
                dshow_controls.CameraControl_Exposure: FakeRangeProp(
                    mn=mn, mx=mx, step=step, default=default
                )
            }
        )


class FakeProcAmp(_BaseControl):
    """Stand-in for IAMVideoProcAmp (gain/brightness)."""

    @classmethod
    def with_gain(
        cls,
        *,
        mn: int = 0,
        mx: int = 100,
        step: int = 1,
        default: int = 0,
    ) -> "FakeProcAmp":
        from palletscan.sources import dshow_controls

        return cls(
            {
                dshow_controls.VideoProcAmp_Gain: FakeRangeProp(
                    mn=mn, mx=mx, step=step, default=default
                ),
                dshow_controls.VideoProcAmp_Brightness: FakeRangeProp(
                    mn=-64, mx=64, step=step, default=0
                ),
            }
        )


class FakeControlFilter:
    """A video-input ``instance`` whose ``QueryInterface`` hands out the fake
    control interfaces, so ``dshow_controls.acquire`` (and therefore the REAL
    ``_command_loop``) works end-to-end against fakes."""

    def __init__(
        self,
        *,
        cam_ctrl: FakeCamCtrl | None = None,
        proc_amp: FakeProcAmp | None = None,
    ) -> None:
        self.cam_ctrl = cam_ctrl
        self.proc_amp = proc_amp

    def QueryInterface(self, iface: type) -> object:  # noqa: N802 - COM naming
        from palletscan.sources import dshow_controls

        if iface is dshow_controls.IAMCameraControl and self.cam_ctrl is not None:
            return self.cam_ctrl
        if iface is dshow_controls.IAMVideoProcAmp and self.proc_amp is not None:
            return self.proc_amp
        raise OSError("E_NOINTERFACE")


# -- pygrabber.dshow_graph fakes ---------------------------------------------


class FakeFilterType(IntEnum):
    """Subset of pygrabber's FilterType enum used by the capture."""

    sample_grabber = 4


class FakeSampleGrabber:
    """Holds the one-shot re-arm flag (``keep_photo``) and the user callback,
    matching the attribute surface PyGrabberCapture touches."""

    def __init__(self, callback: Callable[[np.ndarray], None]) -> None:
        self.callback = self
        self._user_cb = callback
        self.keep_photo = False

    def deliver(self, image: np.ndarray) -> bool:
        """Mirror the real ``SampleGrabberCallback.BufferCB`` one-shot
        contract: an unarmed buffer is DROPPED, and ``keep_photo`` is cleared
        BEFORE the callback runs (the callback must re-arm to keep frames
        flowing). Returns True when the frame was delivered."""
        if not self.keep_photo:
            return False
        self.keep_photo = False
        self._user_cb(image)
        return True


class _FakeFilters(dict):
    """``graph.filters[FilterType.sample_grabber]`` -> object with ``.callback``."""


class FakeVideoInput:
    """pygrabber ``VideoInput``: ``get_formats``/``set_format`` + ``.instance``
    (the COM filter we QueryInterface controls off of)."""

    def __init__(
        self,
        *,
        formats: list[dict],
        instance: object,
        on_set_format: Callable[[int], None] | None = None,
    ) -> None:
        self._formats = formats
        self.instance = instance
        self._on_set_format = on_set_format
        self.format_index: int | None = None

    def get_formats(self) -> list[dict]:
        return self._formats

    def set_format(self, index: int) -> None:
        self.format_index = index
        if self._on_set_format is not None:
            self._on_set_format(index)


class FakeFilterGraph:
    """Scriptable pygrabber ``FilterGraph``.

    The owner thread builds the graph, runs it, then arms the grabber and
    waits for a frame. To model that, :meth:`run` spawns a tiny delivery
    thread that pushes ``frames`` through the registered callback so the
    liveness gate (``self._seq > 0``) is satisfied on the owner thread — the
    same place a real BufferCB would fire.

    Knobs:
      * ``formats`` — what ``get_formats`` returns (entries may carry
        ``min_framerate``/``max_framerate`` to model per-capability rates).
      * ``instance`` — the COM filter handed to ``dshow_controls.acquire``
        (use :class:`FakeControlFilter` for a working QueryInterface path).
      * ``frames`` — frames to deliver after ``run()`` (empty -> dead graph,
        exercises the liveness-timeout path). Each frame is delivered only
        once the grabber is (re-)armed, mirroring the real one-shot BufferCB.
      * ``raise_on`` — name of a build method that should raise (e.g.
        ``"add_video_input_device"``), exercising the build-failure path.
      * ``block_on`` — name of a build method that should BLOCK until the
        test sets ``.unblock`` (bounded wait), simulating a wedged COM call
        for the constructor-timeout lifecycle path.
    """

    instances: list["FakeFilterGraph"] = []

    def __init__(
        self,
        *,
        formats: list[dict] | None = None,
        instance: object | None = None,
        frames: list[np.ndarray] | None = None,
        raise_on: str | None = None,
        block_on: str | None = None,
    ) -> None:
        self._formats = formats if formats is not None else [
            {"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48},
        ]
        self._instance = instance if instance is not None else object()
        self._frames = list(frames) if frames is not None else []
        self._raise_on = raise_on
        self._block_on = block_on
        self.unblock = threading.Event()
        self._device: FakeVideoInput | None = None
        self._grabber: FakeSampleGrabber | None = None
        self.filters: _FakeFilters = _FakeFilters()
        self.running = False
        self.run_called = False
        self.stopped = False
        self.removed = False
        self.delivered = 0
        self._pump_stop = threading.Event()
        self._deliver_thread: threading.Thread | None = None
        FakeFilterGraph.instances.append(self)

    # -- build steps (called in order on the owner thread) ----------------

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise RuntimeError(f"fake graph: {name} failed")

    def _maybe_block(self, name: str) -> None:
        if self._block_on == name:
            # Wedged COM call; the test releases it. Bounded so a bad test
            # can never hang CI.
            self.unblock.wait(timeout=10.0)

    def add_video_input_device(self, index: int) -> None:
        self._maybe_raise("add_video_input_device")
        self._maybe_block("add_video_input_device")
        self._device = FakeVideoInput(
            formats=self._formats, instance=self._instance
        )

    def get_input_device(self) -> FakeVideoInput:
        self._maybe_raise("get_input_device")
        assert self._device is not None
        return self._device

    def add_sample_grabber(self, callback: Callable[[np.ndarray], None]) -> None:
        self._maybe_raise("add_sample_grabber")
        self._maybe_block("add_sample_grabber")
        self._grabber = FakeSampleGrabber(callback)
        self.filters[FakeFilterType.sample_grabber] = self._grabber

    def add_null_render(self) -> None:
        self._maybe_raise("add_null_render")

    def prepare_preview_graph(self) -> None:
        self._maybe_raise("prepare_preview_graph")
        self._maybe_block("prepare_preview_graph")

    def run(self) -> None:
        self.run_called = True  # recorded even when run() then raises
        self._maybe_raise("run")
        self.running = True
        if not self._frames:
            return

        grabber = self._grabber
        assert grabber is not None

        def _pump() -> None:
            # A real driver produces buffers continuously and BufferCB keeps
            # only armed ones. The scripted frame list is finite, so instead of
            # dropping we WAIT (bounded) for the capture to re-arm: frame N+1
            # can only ever be delivered if the callback re-armed after frame
            # N — pumping the whole list proves the one-shot re-arm contract.
            for frame in self._frames:
                deadline = time.monotonic() + 2.0
                while not self._pump_stop.is_set():
                    if grabber.deliver(frame):
                        self.delivered += 1
                        break
                    if time.monotonic() > deadline:
                        return  # capture never re-armed; tests see `delivered`
                    time.sleep(0.001)

        # Deliver from a daemon thread so the owner thread can already be
        # waiting on the liveness condition; daemon + bounded waits keep the
        # test from ever blocking on join.
        self._deliver_thread = threading.Thread(target=_pump, daemon=True)
        self._deliver_thread.start()

    def join_pump(self, timeout: float = 3.0) -> None:
        """Wait for the delivery thread to finish pushing scripted frames."""
        t = self._deliver_thread
        if t is not None:
            t.join(timeout=timeout)

    # -- teardown ---------------------------------------------------------

    def stop(self) -> None:
        self.stopped = True
        self.running = False
        self._pump_stop.set()

    def remove_filters(self) -> None:
        self.removed = True


# -- sys.modules / module-attribute installer ---------------------------------

#: Active installation (None when no fakes are installed). One shared snapshot
#: of the TRUE pre-fake state: a second install() must never re-snapshot the
#: first install's fakes as "saved" (that permanently corrupted sys.modules).
_FAKE_STATE: dict | None = None


def install_fake_directshow(
    graph_factory: Callable[[], FakeFilterGraph],
) -> Callable[[], None]:
    """Install fake ``pygrabber`` (dshow_graph + dshow_ids) modules into
    ``sys.modules`` and a fake ``comtypes`` onto the ``pygrabber_capture``
    module ATTRIBUTE, so the owner thread builds against fakes and never
    touches real COM.

    ``import comtypes`` at the top of ``pygrabber_capture`` binds the module
    global BEFORE any fixture runs, so a ``sys.modules['comtypes']`` swap
    would be dead code — the attribute is what the owner thread resolves.
    The fake records call counts in ``comtypes.calls`` and is marked with
    ``_palletscan_fake`` so tests can prove interception.

    Idempotent: a second install while one is active only retargets the graph
    factory and returns the SAME uninstall handle; the true pre-fake state is
    snapshotted exactly once. ``uninstall`` restores it and is itself
    idempotent (a stale handle is a no-op).
    """
    global _FAKE_STATE
    if _FAKE_STATE is not None:
        _FAKE_STATE["factory"][0] = graph_factory
        return _FAKE_STATE["uninstall"]  # type: ignore[no-any-return]

    from palletscan.sources import pygrabber_capture as _capture_mod

    saved: dict[str, types.ModuleType | None] = {}

    def _swap(name: str, mod: types.ModuleType) -> None:
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    # pygrabber.dshow_graph -- FilterGraph() builds via the CURRENT factory
    # (held in a one-slot list so a repeat install can retarget it).
    # PyGrabberCapture only ever calls ``FilterGraph()``, so a plain callable
    # standing in for the class is sufficient (and keeps mypy happy vs a real
    # __new__ override).
    factory_holder: list[Callable[[], FakeFilterGraph]] = [graph_factory]
    graph_mod = types.ModuleType("pygrabber.dshow_graph")
    graph_mod.FilterGraph = lambda: factory_holder[0]()  # type: ignore[attr-defined]
    graph_mod.FilterType = FakeFilterType  # type: ignore[attr-defined]

    # pygrabber.dshow_ids -- patch_pygrabber_subtypes() needs a `subtypes` dict
    # with pygrabber's own YUY2 entry present (it only setdefault()s).
    ids_mod = types.ModuleType("pygrabber.dshow_ids")
    ids_mod.subtypes = {  # type: ignore[attr-defined]
        "{32595559-0000-0010-8000-00AA00389B71}": "YUY2",
    }

    pkg = types.ModuleType("pygrabber")
    pkg.dshow_graph = graph_mod  # type: ignore[attr-defined]
    pkg.dshow_ids = ids_mod  # type: ignore[attr-defined]

    # comtypes -- counting no-ops, swapped onto the capture module attribute.
    comtypes_mod = types.ModuleType("comtypes")
    calls = {"init": 0, "uninit": 0}
    comtypes_mod.calls = calls  # type: ignore[attr-defined]
    comtypes_mod._palletscan_fake = True  # type: ignore[attr-defined]
    comtypes_mod.CoInitialize = (  # type: ignore[attr-defined]
        lambda: calls.__setitem__("init", calls["init"] + 1)
    )
    comtypes_mod.CoUninitialize = (  # type: ignore[attr-defined]
        lambda: calls.__setitem__("uninit", calls["uninit"] + 1)
    )

    _swap("pygrabber", pkg)
    _swap("pygrabber.dshow_graph", graph_mod)
    _swap("pygrabber.dshow_ids", ids_mod)
    saved_comtypes_attr = _capture_mod.comtypes
    _capture_mod.comtypes = comtypes_mod  # type: ignore[assignment]

    state: dict = {"factory": factory_holder}

    def uninstall() -> None:
        global _FAKE_STATE
        if _FAKE_STATE is not state:
            return  # stale/second handle: already restored
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        _capture_mod.comtypes = saved_comtypes_attr
        _FAKE_STATE = None

    state["uninstall"] = uninstall
    _FAKE_STATE = state
    return uninstall


@pytest.fixture()
def fake_directshow(
    request: pytest.FixtureRequest,
) -> Iterator[Callable[[FakeFilterGraph], None]]:
    """Yield an installer ``install(graph)`` that wires a prepared
    :class:`FakeFilterGraph` into ``sys.modules`` (+ the ``comtypes`` module
    attribute of ``pygrabber_capture``) and guarantees teardown.

    ``install`` may be called more than once in a test (e.g. to build two
    captures against different graphs): repeats retarget the factory without
    re-snapshotting state. The finalizer releases any PyGrabberCapture the
    test registered FIRST (its owner thread must finish against the fakes),
    then uninstalls, so a wedged owner thread can never hang CI.
    """
    FakeFilterGraph.instances.clear()
    uninstall: Callable[[], None] | None = None
    captures: list[object] = []

    def install(graph: FakeFilterGraph) -> None:
        nonlocal uninstall
        uninstall = install_fake_directshow(lambda: graph)

    def register(cap: object) -> None:
        captures.append(cap)

    install.register = register  # type: ignore[attr-defined]

    def _finalize() -> None:
        for cap in captures:
            release = getattr(cap, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
        if uninstall is not None:
            uninstall()

    request.addfinalizer(_finalize)
    yield install
