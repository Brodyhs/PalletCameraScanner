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
    fourcc_str,
    log_reports,
    measure_achieved_fps,
    quirks_for,
    resolve_backend,
)
from palletscan.sources.devices import (
    DeviceInfo,
    backend_flag,
    find_device,
    identity_for_name,
    list_devices,
)
from palletscan.sources.video import packed_luma_channel_for, to_gray
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


def _pygrabber_capture_factory(cfg: CameraConfig) -> CaptureFactory:
    """Capture factory for the pygrabber backend. Lazy-imports pygrabber so
    the dependency is only touched when a pygrabber camera is actually built,
    and closes over ``cfg`` to hand the DirectShow graph its target geometry."""

    def factory(index: int, backend: int) -> Capture:
        from palletscan.sources.pygrabber_capture import PyGrabberCapture

        return PyGrabberCapture(index, width=cfg.width, height=cfg.height)

    return factory


class CameraSource(FrameSource):
    """One live UVC camera, configured and persisted by ``cameras[]`` entry."""

    def __init__(
        self,
        cfg: CameraConfig,
        *,
        capture_factory: CaptureFactory | None = None,
        device_lister: DeviceLister | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        epoch: float | None = None,
        epoch_wall: float | None = None,
    ) -> None:
        self._cfg = cfg
        # Resolved at call time (not def time) so tests can patch the
        # module-level defaults for end-to-end CLI coverage. The pygrabber
        # backend needs the camera's target geometry at construction (the
        # DirectShow format is fixed before the graph runs), so it gets a
        # cfg-closed factory instead of the cv2 default.
        if capture_factory is not None:
            self._capture_factory = capture_factory
        elif cfg.backend is Backend.PYGRABBER:
            self._capture_factory = _pygrabber_capture_factory(cfg)
        else:
            self._capture_factory = default_capture_factory
        self._device_lister = device_lister or list_devices
        self._clock = clock
        # ``epoch`` lets StationRunner anchor every camera's ts=0 at ONE
        # shared instant (sampled before any device is opened), so the
        # cross-camera skew the dedup window compares against is zero by
        # construction (REVIEW finding b8); standalone construction keeps
        # the anchor-at-construction default. Never re-anchored on reopen.
        self._t0 = epoch if epoch is not None else clock()
        #: Wall-clock instant of ts == 0 — the bridge that lets stored
        #: wall_time_iso stamps be compared with this process's source
        #: clock (restart-spanning dedup, finding 10; close-time event
        #: stamping, finding b12). Paired sampling: when the caller passes
        #: ``epoch`` it passes the adjacent wall sample too.
        self.epoch_wall: float = (
            epoch_wall
            if epoch_wall is not None
            else wall_clock() - (clock() - self._t0)
        )
        self._frame_index = 0
        self._closed = False
        self._lock = threading.Lock()
        self._cap: Capture | None = None
        # Packed-YUV luma plane for raw (CONVERT_RGB=0) HxWx2 frames; None
        # means "no packed interpretation known". Seeded from the config,
        # re-derived from the NEGOTIATED format on every (re)connect
        # (REVIEW finding 3).
        self._luma_channel: int | None = packed_luma_channel_for(cfg.fourcc)
        #: Warn-level divergences at (re)connect (unverified controls,
        #: negotiated-vs-configured fourcc): surfaced as the
        #: source.connect_mismatches health metric.
        self.connect_mismatches = 0
        self._shape_checked = False  # first delivered frame per connection
        #: Live device enumeration captured during _resolve(), reused by
        #: _guard_identity() so the identity check costs no extra lister call.
        self._enumerated: list[DeviceInfo] = []
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
        """(index, backend flag) by stable name under the enumeration
        backend; explicit non-enumeration backends (e.g. msmf under
        DSHOW-only enumeration) require a pinned ``fallback_index``.

        A name-resolved index is only valid under the backend it was
        enumerated with (devices.py contract): opening it under another
        backend captures whatever device sits at that DSHOW-ordered slot
        after a replug shifts the order — silently swapping the A/B arms
        (REVIEW finding 8). The fallback_index escape hatch forfeits name
        stability and says so loudly on every connect.
        """
        cfg = self._cfg
        devices = self._device_lister()
        # Stash the live enumeration so _guard_identity() can resolve the
        # expected fingerprint WITHOUT a second lister call (reopen counts
        # lister invocations: one per connect).
        self._enumerated = devices
        flag = backend_flag(cfg.backend)
        if devices:
            dev = find_device(devices, cfg.name)
            if cfg.backend is Backend.AUTO:
                return dev.index, dev.backend
            if flag != dev.backend:
                if cfg.fallback_index is not None:
                    log.warning(
                        "camera %s: explicit backend %s is not the "
                        "enumeration backend; using pinned fallback_index "
                        "%d — name resolution is forfeited and index order "
                        "is NOT stable across replugs (ARRIVAL_CHECKLIST "
                        "step 2)",
                        cfg.id,
                        cfg.backend,
                        cfg.fallback_index,
                    )
                    return cfg.fallback_index, flag
                raise CameraConnectError(
                    f"camera {cfg.id}: name-resolved indexes are only valid "
                    f"under the enumeration backend; opening {cfg.name!r} "
                    f"under explicit backend {cfg.backend} can silently "
                    "capture the wrong physical camera after a replug. Use "
                    "backend: auto/dshow, or pin cameras[].fallback_index "
                    f"to use {cfg.backend} (forfeits name stability)."
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
        backend_name = resolve_backend(cfg.backend).value
        reports = apply_mode(cap, cfg) + apply_settings(
            cap, cfg.settings, quirks_for(cfg.backend), backend_name=backend_name
        )
        log_reports(f"camera {cfg.id} connect", reports)
        # (Re)connect policy: control VALUES may differ silently (warned and
        # counted below — frames at slightly-wrong exposure beat no frames);
        # the frame INTERPRETATION may not. The luma plane follows the
        # format the device actually negotiated, and the first delivered
        # frame is shape-verified in frames() (REVIEW findings 3/8 policy).
        self._apply_negotiated_format(fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)))
        # Loud device-identity line so an operator can catch a wrong-device swap
        # (esp. the color cam under MSMF, which opens by a PINNED INDEX and cannot
        # verify the name — REVIEW finding 9). The first-frame shape gate in
        # frames() rejects a resolution mismatch; this surfaces a same-resolution
        # impostor by making the opened identity visible in the logs.
        log.info(
            "camera %s: OPENED via %s at index %d -> %dx%d fourcc %s (confirm this is %r)",
            cfg.id, cfg.backend.value, index,
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)), cfg.name,
        )
        # Identity guard: dormant under the default policy='warn'. Runs after
        # the OPENED line (so the operator sees what we opened) and BEFORE
        # connect-verify (a wrong-camera swap should be caught before we
        # spend a second sampling its frame rate). May raise under 'strict'.
        self._guard_identity()
        self._shape_checked = False
        # Split the post-connect control accounting into two honest buckets:
        #   (a) genuinely unverifiable-by-backend (verifiable=False) — the
        #       write was asserted but readback can't confirm it on this
        #       backend; INFO, never a mismatch, never logged as 'verified';
        #   (b) actually failed — accepted=False, or a reliable-backend
        #       readback mismatch; WARNING + connect_mismatches (the old path).
        gate_relevant = [r for r in reports if not r.informational]
        unverifiable = [r for r in gate_relevant if not r.verifiable]
        failed = [
            r for r in gate_relevant if r.verifiable and not r.verified
        ]
        if unverifiable:
            log.info(
                "camera %s: %d control(s) asserted but unverifiable on %s: %s",
                cfg.id,
                len(unverifiable),
                backend_name,
                [f"{r.prop}=<{r.requested:g}>" for r in unverifiable],
            )
        if failed:
            # Warn-and-continue on the run path: frames at slightly-wrong
            # exposure beat no frames. Calibrate/selftest are the strict path.
            self.connect_mismatches += 1
            log.warning(
                "camera %s: %d control(s) unverified after connect: %s",
                cfg.id,
                len(failed),
                [r.prop for r in failed],
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

    def _guard_identity(self) -> None:
        """Detect a silent wrong-camera swap (REVIEW finding 9).

        The color See3CAM_24CUG opens BY POSITION under MSMF (pinned index,
        no name check), so a replug reorder or an OBS Virtual Camera at the
        same 1920x1200 swaps in the wrong device under the shape gate's
        radar. We resolve the EXPECTED identity by finding ``cfg.name`` in
        the live DSHOW enumeration and compare it to the calibrated
        fingerprint.

        INDIRECT PROOF, stated plainly: under MSMF the capture index !=
        the DSHOW enumeration index, so we canNOT fingerprint the handle
        OpenCV actually opened. We assert instead that the *enumeration*
        the name resolves to still matches calibration — i.e. it has not
        drifted. A name+identity match means the configured camera is still
        present and consistent with calibration, not a direct read of the
        opened MSMF device.

        Policy ladder (strongest available wins):
          * device_path (when both expected and actual are present)
          * else vid:pid (when both present)
          * else name-only (identity unavailable) — never a strict raise on
            its own UNLESS MSMF is pinned and nothing could be fingerprinted

        policy='off'  -> skip entirely (no resolution work).
        policy='warn' -> DEFAULT, today's exact behavior: a single explicit
            WARNING on mismatch + connect_mismatches; never raises.
        policy='strict' -> raise CameraConnectError on a VID/PID/device_path
            mismatch, OR when MSMF is pinned and NO identity could be obtained
            to even attempt the check.
        """
        cfg = self._cfg
        ident_cfg = cfg.identity
        if ident_cfg.policy == "off":
            return
        strict = ident_cfg.policy == "strict"
        msmf_pinned = resolve_backend(cfg.backend) is Backend.MSMF

        actual = identity_for_name(self._enumerated, cfg.name)
        if actual is None:
            # Identity simply unavailable: macOS/AVFoundation (no property
            # bag), an unreadable DevicePath, or the fallback_index path
            # where enumeration is empty. We proceed on NAME match only and
            # do NOT claim the device is confirmed.
            msg = (
                f"camera {cfg.id}: identity unverifiable on this "
                f"platform/device — proceeding on NAME match only"
            )
            if strict and msmf_pinned and (
                ident_cfg.expected_device_path or ident_cfg.expected_vid_pid
            ):
                # Operator pinned a fingerprint and demanded strict under the
                # by-position MSMF backend, but we cannot obtain ANY identity
                # to check it: refuse to run blind rather than silently
                # opening whatever sits at the pinned index.
                raise CameraConnectError(
                    f"camera {cfg.id}: identity policy 'strict' under MSMF but "
                    "no identity could be obtained to verify the configured "
                    f"name {cfg.name!r} against the pinned fingerprint "
                    "(expected_device_path/expected_vid_pid). Refusing to open "
                    "by position with no identity check. Recalibrate, or set "
                    "identity.policy: warn."
                )
            log.info(msg)
            return

        # Nothing pinned: there is no calibrated fingerprint to compare. We
        # have the actual identity (visible in the OPENED line + logged here)
        # but cannot assert drift. Under strict this is benign — strict gates
        # a *mismatch*, and with no expectation there is nothing to mismatch.
        expected_path = ident_cfg.expected_device_path
        expected_vp = ident_cfg.expected_vid_pid
        if not expected_path and not expected_vp:
            log.info(
                "camera %s: identity present (vid:pid %s, path %r) but no "
                "expected fingerprint pinned — name match only",
                cfg.id,
                f"{actual.vid}:{actual.pid}" if actual.vid else None,
                actual.device_path,
            )
            return

        # Strongest available comparison wins (device_path, else vid:pid).
        mismatch_detail: str | None = None
        if expected_path and actual.device_path:
            if expected_path != actual.device_path:
                mismatch_detail = (
                    f"expected device_path {expected_path!r}, "
                    f"got {actual.device_path!r}"
                )
        elif expected_vp and actual.vid:
            actual_vp = f"{actual.vid}:{actual.pid}"
            if expected_vp != actual_vp:
                mismatch_detail = (
                    f"expected vid:pid {expected_vp}, got {actual_vp}"
                )
        else:
            # An expectation is pinned but the actual device carries no
            # comparable field (e.g. expected a path, device only exposes
            # vid:pid, or vice-versa): cannot confirm OR deny — unverifiable.
            log.info(
                "camera %s: identity unverifiable — pinned fingerprint has no "
                "comparable field on the live device (expected path=%r vid:pid=%s; "
                "actual path=%r vid:pid=%s); proceeding on NAME match only",
                cfg.id,
                expected_path,
                expected_vp,
                actual.device_path,
                f"{actual.vid}:{actual.pid}" if actual.vid else None,
            )
            return

        if mismatch_detail is None:
            confirmed_by = (
                f"device_path {actual.device_path!r}"
                if expected_path and actual.device_path
                else f"vid:pid {actual.vid}:{actual.pid}"
            )
            log.info("camera %s: identity confirmed (%s)", cfg.id, confirmed_by)
            return

        # Mismatch.
        if strict:
            raise CameraConnectError(
                f"camera {cfg.id}: identity MISMATCH — {mismatch_detail}. The "
                f"configured name {cfg.name!r} resolves to a different physical "
                "device than calibration (a replug reorder or a virtual camera "
                "may have taken the slot). Refusing to scan the wrong camera."
            )
        self.connect_mismatches += 1
        log.warning(
            "camera %s: identity MISMATCH — %s. Configured name %r resolves to "
            "a different device than calibration; continuing (policy=warn).",
            cfg.id,
            mismatch_detail,
            cfg.name,
        )

    def _apply_negotiated_format(self, negotiated: str) -> None:
        """Derive the packed-YUV luma channel from the NEGOTIATED fourcc.

        UVC devices lie: a requested mode may be silently snapped to the
        sibling packed format (UYVY <-> YUY2), and the run path's mode
        readback is warn-only — fixing the channel from the *configured*
        value made such a camera read the chroma plane as "grayscale" and
        go blind without a single failed check (REVIEW finding 3).

        Unverifiable readback (0.0 or garbage renders as '?') falls back to
        the configured value, warned and counted. A format with no packed
        interpretation leaves the channel None; whether that matters is
        decided by the first delivered frame's actual shape (frames()), not
        by string knowledge — a mono camera delivering 2-D frames needs no
        channel at all.
        """
        cfg = self._cfg
        norm = negotiated.strip().upper()
        configured = (cfg.fourcc or "").strip().upper() or None
        if not norm or "?" in norm:
            if configured is not None:
                self.connect_mismatches += 1
                log.warning(
                    "camera %s: fourcc readback unverifiable (%r); deriving "
                    "the luma layout from the configured %s",
                    cfg.id,
                    negotiated,
                    configured,
                )
            self._luma_channel = packed_luma_channel_for(configured)
            return
        if configured is not None and norm != configured:
            self.connect_mismatches += 1
            log.warning(
                "camera %s: device negotiated %s instead of the configured "
                "%s; the luma layout follows the NEGOTIATED format",
                cfg.id,
                norm,
                configured,
            )
        channel = packed_luma_channel_for(norm)
        if channel is None:
            channel = packed_luma_channel_for(configured)
        self._luma_channel = channel

    def _verify_frame_shape(self, img: np.ndarray) -> None:
        """First-delivered-frame gate, once per connection.

        The delivered frame is the only honest format oracle (readback
        lies, see probe.py). Interpretation-bearing mismatches fail loudly
        into the watchdog's retry path instead of silently scanning the
        wrong pixels or the wrong optics envelope (REVIEW findings 3/8
        policy: what may differ silently vs what must fail).
        """
        cfg = self._cfg
        if img.ndim not in (2, 3):
            raise CameraReadError(
                f"camera {cfg.id}: undecodable frame layout {img.shape}; "
                "the negotiated format does not deliver image frames — "
                "check cameras[].fourcc/convert_rgb"
            )
        if img.ndim == 3 and img.shape[2] == 2 and self._luma_channel is None:
            raise CameraReadError(
                f"camera {cfg.id}: device delivers packed 2-channel frames "
                "but no luma layout is known for the negotiated/configured "
                "format; refusing to scan a chroma plane as luma. Set "
                "cameras[].fourcc to the actual format (UYVY/YUY2) or "
                "enable convert_rgb."
            )
        h, w = int(img.shape[0]), int(img.shape[1])
        if (cfg.width is not None and w != cfg.width) or (
            cfg.height is not None and h != cfg.height
        ):
            raise CameraReadError(
                f"camera {cfg.id}: device delivers {w}x{h} frames but the "
                f"locked mode is {cfg.width}x{cfg.height}; a silently "
                "different geometry corrupts the optics envelope and the "
                "A/B attribution. Recalibrate (palletscan calibrate --save) "
                "or clear cameras[].width/height to accept the device "
                "default."
            )

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
            if not self._shape_checked:
                self._verify_frame_shape(img)
                self._shape_checked = True
            ts = self._clock() - self._t0  # sampled right after read()
            # Allocate the index before yielding: a generator abandoned
            # mid-yield (watchdog reopen) must never reissue an index.
            idx = self._frame_index
            self._frame_index += 1
            yield Frame(
                image=to_gray(
                    img,
                    packed_luma_channel=(
                        0 if self._luma_channel is None else self._luma_channel
                    ),
                ),
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
    wall_clock: Callable[[], float] = time.time,
    epoch: float | None = None,
    epoch_wall: float | None = None,
) -> FrameSource:
    """Resolve ``source.camera``, build the CameraSource, wrap it in the
    reliability watchdog. ``create_source`` calls this with defaults;
    tests inject fakes through the same seams. ``epoch``/``epoch_wall``
    are StationRunner's shared clock anchor (REVIEW finding b8)."""
    from palletscan.reliability.watchdog import WatchdogSource

    cam_cfg = resolve_camera(cfg)
    inner = CameraSource(
        cam_cfg,
        capture_factory=capture_factory,
        device_lister=device_lister,
        clock=clock,
        wall_clock=wall_clock,
        epoch=epoch,
        epoch_wall=epoch_wall,
    )
    return WatchdogSource(inner, cfg.watchdog)
