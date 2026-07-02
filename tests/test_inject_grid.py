"""inject_grid's shared-camera adapter: sequential grid points must each get
a fresh stream from the ONE open device.

Regression: the adapter used to wrap build_camera_source()'s return — the
WatchdogSource, whose frames() is strictly single-use and raises on reuse —
so the shared-camera grid failed on the second grid point. The fixed
_build_shared_inner wraps the bare re-iterable CameraSource (the adapter's
own documented contract); these tests drive two point-lifecycles against
scripted camera fakes.
"""

from __future__ import annotations

import cv2

from palletscan.config import AppConfig, Backend, CameraConfig
from palletscan.sources.devices import devices_from_names
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock
from tools.inject_grid import _build_shared_inner


def _app_cfg() -> AppConfig:
    return AppConfig(
        cameras=[
            CameraConfig(
                id="cam-test",
                name="See3CAM_24CUG",
                backend=Backend.MSMF,
                connect_verify_s=0.0,
            )
        ]
    )


def test_shared_inner_survives_two_sequential_grid_points() -> None:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    shared = _build_shared_inner(
        _app_cfg(),
        capture_factory=factory,
        device_lister=lambda: devices_from_names(
            ["See3CAM_24CUG"], int(cv2.CAP_MSMF)
        ),
        clock=clock,
    )
    try:
        # grid point 1 consumes frames; its per-point runner then close()s
        # the source, which must NOT tear down the shared device
        it1 = shared.frames()
        first = [next(it1) for _ in range(3)]
        it1.close()
        shared.close()  # per-point close: a no-op by contract
        # grid point 2 must get a fresh, working stream from the same device
        it2 = shared.frames()
        more = [next(it2) for _ in range(3)]
        it2.close()
    finally:
        shared.shutdown()
    assert [f.frame_index for f in first] == [0, 1, 2]
    assert [f.frame_index for f in more] == [3, 4, 5]  # same open connection
    assert len(factory.created) == 1  # ONE device open across both points
    assert factory.created[0].release_calls >= 1  # shutdown really released
