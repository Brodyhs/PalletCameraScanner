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

* :func:`fake_directshow` — a fixture that installs the fake ``pygrabber`` /
  ``comtypes`` modules into ``sys.modules`` BEFORE PyGrabberCapture is built,
  and RELEASES them in a finalizer so CI can never hang on a leaked module or
  a live owner thread.
"""

from __future__ import annotations

import sys
import threading
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

    def deliver(self, image: np.ndarray) -> None:
        """Invoke the registered frame callback (as the BufferCB would)."""
        self._user_cb(image)


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
      * ``formats`` — what ``get_formats`` returns.
      * ``instance`` — the COM filter handed to ``dshow_controls.acquire``.
      * ``frames`` — frames to deliver after ``run()`` (empty -> dead graph,
        exercises the liveness-timeout path).
      * ``raise_on`` — name of a build method that should raise (e.g.
        ``"add_video_input_device"``), exercising the build-failure path.
    """

    instances: list["FakeFilterGraph"] = []

    def __init__(
        self,
        *,
        formats: list[dict] | None = None,
        instance: object | None = None,
        frames: list[np.ndarray] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self._formats = formats if formats is not None else [
            {"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48},
        ]
        self._instance = instance if instance is not None else object()
        self._frames = list(frames) if frames is not None else []
        self._raise_on = raise_on
        self._device: FakeVideoInput | None = None
        self._grabber: FakeSampleGrabber | None = None
        self.filters: _FakeFilters = _FakeFilters()
        self.running = False
        self.stopped = False
        self.removed = False
        self._deliver_thread: threading.Thread | None = None
        FakeFilterGraph.instances.append(self)

    # -- build steps (called in order on the owner thread) ----------------

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise RuntimeError(f"fake graph: {name} failed")

    def add_video_input_device(self, index: int) -> None:
        self._maybe_raise("add_video_input_device")
        self._device = FakeVideoInput(
            formats=self._formats, instance=self._instance
        )

    def get_input_device(self) -> FakeVideoInput:
        self._maybe_raise("get_input_device")
        assert self._device is not None
        return self._device

    def add_sample_grabber(self, callback: Callable[[np.ndarray], None]) -> None:
        self._maybe_raise("add_sample_grabber")
        self._grabber = FakeSampleGrabber(callback)
        self.filters[FakeFilterType.sample_grabber] = self._grabber

    def add_null_render(self) -> None:
        self._maybe_raise("add_null_render")

    def prepare_preview_graph(self) -> None:
        self._maybe_raise("prepare_preview_graph")

    def run(self) -> None:
        self._maybe_raise("run")
        self.running = True
        if not self._frames:
            return

        grabber = self._grabber
        assert grabber is not None

        def _pump() -> None:
            for frame in self._frames:
                # Mirror the real one-shot grabber: only deliver while armed.
                grabber.deliver(frame)

        # Deliver synchronously after a microscopic delay so the owner thread
        # is already waiting on the liveness condition. A daemon thread keeps
        # the test from ever blocking on join.
        self._deliver_thread = threading.Thread(target=_pump, daemon=True)
        self._deliver_thread.start()

    # -- teardown ---------------------------------------------------------

    def stop(self) -> None:
        self.stopped = True
        self.running = False

    def remove_filters(self) -> None:
        self.removed = True


# -- sys.modules installer ----------------------------------------------------


def install_fake_directshow(
    graph_factory: Callable[[], FakeFilterGraph],
) -> Callable[[], None]:
    """Install fake ``pygrabber`` (dshow_graph + dshow_ids) and ``comtypes``
    modules into ``sys.modules`` so the owner thread builds against fakes.

    Returns a no-arg ``uninstall`` callable that restores the prior modules.
    PyGrabberCapture does ``from pygrabber.dshow_graph import FilterGraph,
    FilterType`` and ``import comtypes`` inside the owner thread, so the fakes
    must already be installed before construction.
    """
    saved: dict[str, types.ModuleType | None] = {}

    def _swap(name: str, mod: types.ModuleType | None) -> None:
        saved[name] = sys.modules.get(name)
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod

    # pygrabber.dshow_graph -- FilterGraph() builds via the supplied factory.
    # PyGrabberCapture only ever calls ``FilterGraph()``, so a plain callable
    # standing in for the class is sufficient (and keeps mypy happy vs a real
    # __new__ override).
    graph_mod = types.ModuleType("pygrabber.dshow_graph")
    graph_mod.FilterGraph = lambda: graph_factory()  # type: ignore[attr-defined]
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

    # comtypes -- CoInitialize/CoUninitialize are no-ops here.
    comtypes_mod = types.ModuleType("comtypes")
    comtypes_mod.CoInitialize = lambda: None  # type: ignore[attr-defined]
    comtypes_mod.CoUninitialize = lambda: None  # type: ignore[attr-defined]

    _swap("pygrabber", pkg)
    _swap("pygrabber.dshow_graph", graph_mod)
    _swap("pygrabber.dshow_ids", ids_mod)
    _swap("comtypes", comtypes_mod)

    def uninstall() -> None:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    return uninstall


@pytest.fixture()
def fake_directshow(
    request: pytest.FixtureRequest,
) -> Iterator[Callable[[FakeFilterGraph], None]]:
    """Yield an installer ``install(graph)`` that wires a prepared
    :class:`FakeFilterGraph` into ``sys.modules`` and guarantees teardown.

    The finalizer always uninstalls the fake modules (so later tests see the
    real pygrabber/comtypes) and releases any PyGrabberCapture the test built,
    so a wedged owner thread can never hang CI.
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
