"""CameraSource: live UVC capture behind the FrameSource seam.

Construction **fails fast** (consistent with VideoFileSource and the
"refuse to run blind" posture): the device must enumerate by name, open,
and take its mode/settings before the pipeline starts. Every failure
*after* start is the reliability watchdog's job — it calls
:meth:`CameraSource.reopen`, which re-enumerates by name and re-applies
the persisted settings on every attempt (UVC controls reset on
re-enumeration, spec §2/§5).

Timestamp semantics: ``ts = clock() - t0``, sampled right after
``read()`` returns; ``t0`` is anchored once at construction and **never
re-anchored on reopen**, so ts stays monotonic across reconnects and an
outage appears as a real gap in source time — which is what dedup
windows and miss deadlines should see. ``CAP_PROP_POS_MSEC`` is not used
(unreliable for live devices). ``frame_index`` likewise increments
monotonically across reopens (same convention as video looping).

The only place a real ``cv2.VideoCapture`` is born is the default
``capture_factory``; tests inject fakes through the same constructor
seams (no cv2 monkeypatching).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol

import cv2
import numpy as np

from palletscan.config import AppConfig, Backend, CameraConfig, resolve_camera
from palletscan.sources.base import FrameSource
from palletscan.sources.controls import (
    all_verified,
    apply_mode,
    apply_settings,
    log_reports,
    measure_achieved_fps,
    quirks_for,
)
from palletscan.sources.devices import DeviceInfo, backend_flag, find_device, list_devices
from palletscan.sources.video import to_gray
from palletscan.types import Frame

log = logging.getLogger(__name__)

#: Pause between consecutive failed reads (no hot-spin on a glitching device).
_READ_RETRY_SLEEP_S = 0.005

#: Connect-verify tolerance: warn when achieved fps undershoots this
#: fraction of the configured rate (run path is warn-only; selftest has
#: its own, harder gate).
_CONNECT_FPS_FRACTION = 0.8


class CameraConnectError(RuntimeError):
    """Device failed to enumerate/open/configure during (re)connect."""


class CameraReadError(RuntimeError):
    """The device stopped delivering frames (consecutive read failures)."""


class Capture(Protocol):
    """Structural protocol for cv2.VideoCapture (and test fakes)."""

    def isOpened(self) -> bool: ...  # noqa: N802 - cv2 naming

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def set(self, prop: int, value: float) -> bool: ...

    def get(self, prop: int) -> float: ...

    def release(self) -> None: ...


CaptureFactory = Callable[[int, int], Capture]
DeviceLister = Callable[[], list[DeviceInfo]]


def default_capture_factory(index: int, backend: int) -> Capture:
    return cv2.VideoCapture(index, backend)


class CameraSource(FrameSource):
    """One live UVC camera, configured and persisted by ``cameras[]`` entry."""

    def __init__(
        self,
        cfg: CameraConfig,
        *,
        capture_factory: CaptureFactory | None = None,
        device_lister: DeviceLister | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        # Resolved at call time (not def time) so tests can patch the
        # module-level defaults for end-to-end CLI coverage.
        self._capture_factory = capture_factory or default_capture_factory
        self._device_lister = device_lister or list_devices
        self._clock = clock
        self._t0 = clock()  # anchored once; never re-anchored on reopen
        self._frame_index = 0
        self._closed = False
        self._lock = threading.Lock()
        self._cap: Capture | None = None
        # Packed-YUV luma plane for raw (CONVERT_RGB=0) HxWx2 frames:
        # UYVY interleaves U Y V Y, so its luma is channel 1; YUY2 is 0.
        self._luma_channel = 1 if (cfg.fourcc or "").upper() == "UYVY" else 0
        self._connect()  # fail fast: refuse to run blind

    @property
    def source_id(self) -> str:
        return self._cfg.id

    @property
    def nominal_fps(self) -> float | None:
        return self._cfg.fps

    @property
    def live(self) -> bool:
        return True

    # -- (re)connect -------------------------------------------------------

    def _resolve(self) -> tuple[int, int]:
        """(index, backend flag) by stable name; fallback_index only when
        the platform yields no names at all."""
        cfg = self._cfg
        devices = self._device_lister()
        flag = backend_flag(cfg.backend)
        if devices:
            dev = find_device(devices, cfg.name)
            if cfg.backend is Backend.AUTO:
                return dev.index, dev.backend
            if flag != dev.backend:
                log.warning(
                    "camera %s: explicit backend %s differs from the "
                    "enumeration backend; device order may not match "
                    "(ARRIVAL_CHECKLIST step 2)",
                    cfg.id,
                    cfg.backend,
                )
            return dev.index, flag
        if cfg.fallback_index is not None:
            log.warning(
                "camera %s: no devices enumerated by name; falling back to "
                "bare index %d — index order is NOT stable across replugs",
                cfg.id,
                cfg.fallback_index,
            )
            return cfg.fallback_index, flag
        raise CameraConnectError(
            f"camera {cfg.id}: no devices enumerated and no fallback_index "
            f"configured (looking for name {cfg.name!r})"
        )

    def _connect(self) -> None:
        """Enumerate -> open -> mode -> settings -> verify. Shared by
        construction and :meth:`reopen` (settings re-apply every time)."""
        cfg = self._cfg
        index, flag = self._resolve()
        cap = self._capture_factory(index, flag)
        if not cap.isOpened():
            cap.release()
            raise CameraConnectError(
                f"camera {cfg.id}: device {cfg.name!r} at index {index} "
                f"(backend {flag}) did not open"
            )
        # Publish the in-flight capture BEFORE any blocking device I/O
        # (set/get, connect-verify reads): close() from another thread must
        # be able to release a capture wedged inside the connect sequence,
        # or a hung driver here would freeze the watchdog's consumer thread
        # with no unblock path.
        with self._lock:
            if self._closed:
                cap.release()
                raise CameraConnectError(f"camera {cfg.id}: closed during connect")
            self._cap = cap
        reports = apply_mode(cap, cfg) + apply_settings(
            cap, cfg.settings, quirks_for(cfg.backend)
        )
        log_reports(f"camera {cfg.id} connect", reports)
        if not all_verified(reports):
            # Warn-and-continue on the run path: frames at slightly-wrong
            # exposure beat no frames. Calibrate/selftest are the strict path.
            unverified = [
                r.prop for r in reports if not (r.verified or r.informational)
            ]
            log.warning(
                "camera %s: %d control(s) unverified after connect: %s",
                cfg.id,
                len(unverified),
                unverified,
            )
        if cfg.connect_verify_s > 0:
            m = measure_achieved_fps(
                cap, sample_s=cfg.connect_verify_s, clock=self._clock
            )
            log.info(
                "camera %s: connect-verify %.1f fps over %.1fs (%d frames)",
                cfg.id,
                m.fps,
                m.elapsed_s,
                m.frames,
            )
            if cfg.fps is not None and m.fps < _CONNECT_FPS_FRACTION * cfg.fps:
                log.warning(
                    "camera %s: achieved %.1f fps is below %.0f%% of the "
                    "configured %.1f fps",
                    cfg.id,
                    m.fps,
                    _CONNECT_FPS_FRACTION * 100,
                    cfg.fps,
                )
        if self._closed:
            # close() raced the connect: the published capture is already
            # released; report the connect as failed rather than succeeded.
            raise CameraConnectError(f"camera {cfg.id}: closed during connect")

    def reopen(self) -> None:
        """Watchdog recovery hook: tear down, re-enumerate by name,
        re-apply persisted settings. The ts anchor survives."""
        with self._lock:
            old, self._cap = self._cap, None
            self._closed = False
        if old is not None:
            old.release()
        self._connect()

    # -- streaming -----------------------------------------------------------

    def frames(self) -> Iterator[Frame]:
        """Yield frames until close/failure. Single-use **per connection**:
        the iterator binds the current capture, so an abandoned iterator
        from before a reopen can never steal frames from the new one."""
        cfg = self._cfg
        with self._lock:
            cap = self._cap
        if cap is None:
            raise CameraReadError(f"camera {cfg.id}: not connected")
        consecutive_fails = 0
        while not self._closed:
            ok, img = cap.read()
            # Stale-connection check, not just _closed: reopen() resets
            # _closed before an abandoned (zombie) reader wakes, so a woken
            # zombie must also see that its capture was replaced — otherwise
            # it would race the live iterator for _frame_index.
            if self._closed or self._cap is not cap:
                break
            if not ok or img is None:
                consecutive_fails += 1
                if consecutive_fails >= cfg.read_fail_limit:
                    raise CameraReadError(
                        f"camera {cfg.id}: {consecutive_fails} consecutive "
                        "read failures"
                    )
                time.sleep(_READ_RETRY_SLEEP_S)
                continue
            consecutive_fails = 0
            ts = self._clock() - self._t0  # sampled right after read()
            # Allocate the index before yielding: a generator abandoned
            # mid-yield (watchdog reopen) must never reissue an index.
            idx = self._frame_index
            self._frame_index += 1
            yield Frame(
                image=to_gray(img, packed_luma_channel=self._luma_channel),
                ts=ts,
                frame_index=idx,
                source_id=cfg.id,
            )

    def close(self) -> None:
        """Idempotent; callable from another thread — ``release()`` is the
        documented way to unblock a capture stuck in ``read()``."""
        self._closed = True
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()


def build_camera_source(
    cfg: AppConfig,
    *,
    capture_factory: CaptureFactory | None = None,
    device_lister: DeviceLister | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FrameSource:
    """Resolve ``source.camera``, build the CameraSource, wrap it in the
    reliability watchdog. ``create_source`` calls this with defaults;
    tests inject fakes through the same seams."""
    from palletscan.reliability.watchdog import WatchdogSource

    cam_cfg = resolve_camera(cfg)
    inner = CameraSource(
        cam_cfg,
        capture_factory=capture_factory,
        device_lister=device_lister,
        clock=clock,
    )
    return WatchdogSource(inner, cfg.watchdog)
