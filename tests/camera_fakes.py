"""Scriptable fakes for the camera stack (no cv2.VideoCapture monkeypatching).

``FakeCapture`` satisfies the :class:`palletscan.sources.camera.Capture`
protocol structurally and is injected through the same
``capture_factory``/``device_lister`` constructor seams production uses,
so tests exercise the real wiring — only the device is fake.

Backend quirk *profiles* (DSHOW log2-quantized exposure, MSMF 0.25/0.75
auto-exposure semantics, AVFoundation ignoring control sets) are hook
dicts, mirroring the QUIRKS data in ``palletscan/sources/controls.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import cv2
import numpy as np

#: Default property store of a freshly opened fake device.
_DEFAULT_PROPS: dict[int, float] = {
    cv2.CAP_PROP_FRAME_WIDTH: 640.0,
    cv2.CAP_PROP_FRAME_HEIGHT: 480.0,
    cv2.CAP_PROP_FPS: 30.0,
    cv2.CAP_PROP_FOURCC: float(cv2.VideoWriter_fourcc(*"YUY2")),
}


class FakeClock:
    """Injectable monotonic clock shared by fakes and code under test."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def default_frame(cap: "FakeCapture") -> np.ndarray:
    """BGR frame whose mean brightness tracks the exposure property
    (8 counts per exposure stop around 128), so exposure verification has
    a physically-plausible effect to measure."""
    exposure = cap.props.get(cv2.CAP_PROP_EXPOSURE)
    base = 128.0 if exposure is None else 128.0 + 8.0 * exposure
    base += cap.props.get(cv2.CAP_PROP_BRIGHTNESS, 0.0)
    h = int(cap.props[cv2.CAP_PROP_FRAME_HEIGHT])
    w = int(cap.props[cv2.CAP_PROP_FRAME_WIDTH])
    return np.full((h, w, 3), np.clip(base, 0, 255), np.uint8)


class FakeCapture:
    """Scriptable stand-in for ``cv2.VideoCapture``.

    ``hooks`` maps prop id -> ``f(value) -> accepted | None``: the device
    may quantize a set (return a different value) or reject/ignore it
    (return None -> ``set()`` returns False, store unchanged).

    ``read_script`` items, consumed one per ``read()`` then ``after``
    forever: ``"ok"`` (frame), ``"fail"`` ((False, None)), ``"hang"``
    (block until :meth:`release` — the unblock contract cv2 honors),
    ``"zombie"`` (block until ``zombie_escape`` is set — release does
    *not* unblock; the wedged-driver case; delivers a frame once escaped
    so stale-generation discarding can be exercised), or an exception
    instance to raise.

    A shared ``clock`` plus ``real_fps`` makes every ``read()`` advance
    the injected clock by one frame interval, so achieved-fps sampling is
    deterministic and instant.
    """

    def __init__(
        self,
        *,
        opened: bool = True,
        props: dict[int, float] | None = None,
        hooks: dict[int, Callable[[float], float | None]] | None = None,
        read_script: tuple | list = (),
        after: str = "ok",
        frame_factory: Callable[["FakeCapture"], np.ndarray] = default_frame,
        clock: FakeClock | None = None,
        real_fps: float | Callable[["FakeCapture"], float] | None = None,
    ) -> None:
        self.opened = opened
        self.props: dict[int, float] = dict(_DEFAULT_PROPS)
        if props:
            self.props.update(props)
        self.hooks = dict(hooks or {})
        self._script = list(read_script)
        self._after = after
        self._frame_factory = frame_factory
        self._clock = clock
        self._real_fps = real_fps
        self.set_calls: list[tuple[int, float]] = []
        self.reads = 0
        self.release_calls = 0
        self.released = threading.Event()
        self.zombie_escape = threading.Event()

    # -- Capture protocol -------------------------------------------------

    def isOpened(self) -> bool:  # noqa: N802 - cv2 naming
        return self.opened

    def set(self, prop: int, value: float) -> bool:
        self.set_calls.append((prop, float(value)))
        hook = self.hooks.get(prop)
        if hook is not None:
            accepted = hook(float(value))
            if accepted is None:
                return False
            self.props[prop] = float(accepted)
            return True
        self.props[prop] = float(value)
        return True

    def get(self, prop: int) -> float:
        return float(self.props.get(prop, 0.0))  # cv2: 0.0 = unsupported

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.reads += 1
        if self._clock is not None and self._real_fps is not None:
            fps = (
                self._real_fps(self)
                if callable(self._real_fps)
                else self._real_fps
            )
            self._clock.advance(1.0 / fps)
        item: object = self._script.pop(0) if self._script else self._after
        if isinstance(item, BaseException):
            raise item
        if item == "fail" or not self.opened:
            return False, None
        if item == "hang":
            self.released.wait()
            return False, None
        if item == "zombie":
            self.zombie_escape.wait()
        return True, self._frame_factory(self)

    def release(self) -> None:
        self.release_calls += 1
        self.opened = False
        self.released.set()

    # -- conveniences ------------------------------------------------------

    def sets_for(self, prop: int) -> list[float]:
        return [v for p, v in self.set_calls if p == prop]


class FakeCaptureFactory:
    """Recording ``capture_factory(index, backend_flag)``.

    ``captures`` is consumed in order (an item may be a FakeCapture or a
    ``f(index, backend) -> FakeCapture``); when exhausted, ``default``
    builds the rest. Scripts like ``[FakeCapture(opened=False),
    FakeCapture()]`` model fail-then-succeed reopens.
    """

    def __init__(
        self,
        captures: list | None = None,
        default: Callable[[int, int], FakeCapture] | None = None,
    ) -> None:
        self._script = list(captures or [])
        self._default = default or (lambda index, backend: FakeCapture())
        self.calls: list[tuple[int, int]] = []
        self.created: list[FakeCapture] = []

    def __call__(self, index: int, backend: int) -> FakeCapture:
        self.calls.append((index, backend))
        item = self._script.pop(0) if self._script else self._default
        cap = item(index, backend) if callable(item) else item
        self.created.append(cap)
        return cap


# -- backend quirk profiles ---------------------------------------------------


def dshow_hooks() -> dict[int, Callable[[float], float | None]]:
    """DirectShow: exposure is log2 stops, quantized to whole stops by the
    driver; auto-exposure accepts only the backend's on/off values."""
    return {
        cv2.CAP_PROP_EXPOSURE: lambda v: float(round(v)),
        cv2.CAP_PROP_AUTO_EXPOSURE: lambda v: v if v in (0.0, 1.0) else None,
    }


def msmf_hooks() -> dict[int, Callable[[float], float | None]]:
    """MSMF: exposure accepted verbatim; auto-exposure is 0.25 manual /
    0.75 auto, anything else rejected."""
    return {
        cv2.CAP_PROP_AUTO_EXPOSURE: lambda v: v if v in (0.25, 0.75) else None,
    }


def avfoundation_hooks() -> dict[int, Callable[[float], float | None]]:
    """AVFoundation mostly ignores UVC control properties."""

    def _ignore(_: float) -> None:
        return None

    return {
        cv2.CAP_PROP_EXPOSURE: _ignore,
        cv2.CAP_PROP_AUTO_EXPOSURE: _ignore,
        cv2.CAP_PROP_GAIN: _ignore,
        cv2.CAP_PROP_BRIGHTNESS: _ignore,
    }
