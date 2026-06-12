"""Camera control layer: set a value, read it back, verify the effect.

OpenCV's UVC control handling is backend-quirky (spec §2): DirectShow
exposes exposure as log2-scaled stops, MSMF flips auto-exposure with
0.25/0.75 magic values, AVFoundation mostly ignores control properties.
That knowledge is **data** — the :data:`QUIRKS` table — kept in one place
so arrival-day findings correct a constant, not scattered logic.

Every property write produces an honest :class:`ControlReport`
(requested vs accepted vs read-back); callers decide severity. At
run/(re)connect the policy is warn-and-continue (frames at slightly-wrong
exposure beat no frames); calibrate/selftest hard-fail on
``controls_reliable`` backends and warn honestly on AVFoundation.

``verify_exposure_effect`` perturbs the camera, so it runs in
calibrate/selftest only — never on the live path.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from palletscan.config import Backend, CameraConfig, CameraSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackendQuirks:
    """Per-backend control semantics (corrected on arrival day if reality
    disagrees — ARRIVAL_CHECKLIST step 3)."""

    auto_exposure_on: float
    auto_exposure_off: float
    exposure_is_log2: bool
    controls_reliable: bool


#: Backend quirk knowledge as data. DSHOW: log2 exposure stops, classic
#: 1/0 auto toggle. MSMF: 0.25 manual / 0.75 auto. AVFoundation ignores
#: most UVC control props (``controls_reliable=False`` keeps dev-machine
#: calibration honest instead of hard-failing). AUTO covers CAP_ANY on
#: platforms with no named entry.
QUIRKS: dict[Backend, BackendQuirks] = {
    Backend.DSHOW: BackendQuirks(
        auto_exposure_on=1.0,
        auto_exposure_off=0.0,
        exposure_is_log2=True,
        controls_reliable=True,
    ),
    Backend.MSMF: BackendQuirks(
        auto_exposure_on=0.75,
        auto_exposure_off=0.25,
        exposure_is_log2=False,
        controls_reliable=True,
    ),
    Backend.AVFOUNDATION: BackendQuirks(
        auto_exposure_on=1.0,
        auto_exposure_off=0.0,
        exposure_is_log2=False,
        controls_reliable=False,
    ),
    Backend.AUTO: BackendQuirks(
        auto_exposure_on=0.75,
        auto_exposure_off=0.25,
        exposure_is_log2=False,
        controls_reliable=True,
    ),
}


def resolve_backend(backend: Backend, platform: str = sys.platform) -> Backend:
    """Resolve AUTO to the platform's default backend."""
    if backend is not Backend.AUTO:
        return backend
    if platform == "win32":
        return Backend.DSHOW
    if platform == "darwin":
        return Backend.AVFOUNDATION
    return Backend.AUTO


def quirks_for(backend: Backend, platform: str = sys.platform) -> BackendQuirks:
    return QUIRKS[resolve_backend(backend, platform)]


def fourcc_float(code: str) -> float:
    """'UYVY' -> the float cv2 wants for CAP_PROP_FOURCC."""
    return float(cv2.VideoWriter.fourcc(*code))


def fourcc_str(value: float) -> str:
    """CAP_PROP_FOURCC readback -> 'UYVY' (non-printable bytes escaped)."""
    iv = int(value) & 0xFFFFFFFF
    chars = [chr((iv >> (8 * i)) & 0xFF) for i in range(4)]
    return "".join(c if c.isprintable() else "?" for c in chars)


@dataclass(frozen=True, slots=True)
class ControlReport:
    """Honest per-property outcome of one ``set``."""

    prop: str
    requested: float
    accepted: bool  # what cap.set() claimed
    readback: float
    verified: bool  # readback matches the request (within quirk tolerance)
    note: str = ""
    #: Best-effort property (e.g. BUFFERSIZE, which DSHOW/MSMF do not
    #: implement): reported honestly but never fed to a hard pass/fail gate.
    informational: bool = False


def all_verified(reports: list[ControlReport]) -> bool:
    """True when every gate-relevant report verified (informational
    best-effort properties report honestly but never fail a gate)."""
    return all(r.verified or r.informational for r in reports)


def log_reports(label: str, reports: list[ControlReport]) -> None:
    """One structured JSON-lines entry for the whole report list."""
    log.info(
        "%s: %d/%d controls verified",
        label,
        sum(r.verified for r in reports),
        len(reports),
        extra={"stats": {"controls": [asdict(r) for r in reports]}},
    )


def _set(
    cap,
    name: str,
    prop: int,
    requested: float,
    *,
    tol: float = 1e-3,
    quantize_tol: float | None = None,
    note: str = "",
    informational: bool = False,
) -> ControlReport:
    accepted = bool(cap.set(prop, requested))
    readback = float(cap.get(prop))
    verified = abs(readback - requested) <= tol
    if not verified and quantize_tol is not None:
        if abs(readback - requested) <= quantize_tol:
            verified = True
            note = (note + " " if note else "") + f"quantized to {readback:g}"
    if not accepted:
        verified = False
        note = (note + " " if note else "") + "set() rejected"
    elif not verified:
        note = (note + " " if note else "") + f"readback {readback:g}"
    return ControlReport(
        prop=name,
        requested=float(requested),
        accepted=accepted,
        readback=readback,
        verified=verified,
        note=note.strip(),
        informational=informational,
    )


def apply_mode(cap, cam: CameraConfig) -> list[ControlReport]:
    """Apply format/resolution/fps **in this order**: FOURCC, width, height,
    fps, CONVERT_RGB, BUFFERSIZE — a UVC mode change can reset what came
    before it, so controls (:func:`apply_settings`) always come after."""
    reports: list[ControlReport] = []
    if cam.fourcc is not None:
        requested = fourcc_float(cam.fourcc)
        rep = _set(cap, "fourcc", cv2.CAP_PROP_FOURCC, requested)
        if not rep.verified:
            rep = ControlReport(
                **{
                    **asdict(rep),
                    "note": f"requested {cam.fourcc}, got {fourcc_str(rep.readback)}",
                }
            )
        reports.append(rep)
    if cam.width is not None:
        reports.append(
            _set(cap, "width", cv2.CAP_PROP_FRAME_WIDTH, float(cam.width))
        )
    if cam.height is not None:
        reports.append(
            _set(cap, "height", cv2.CAP_PROP_FRAME_HEIGHT, float(cam.height))
        )
    if cam.fps is not None:
        reports.append(_set(cap, "fps", cv2.CAP_PROP_FPS, float(cam.fps)))
    reports.append(
        _set(
            cap,
            "convert_rgb",
            cv2.CAP_PROP_CONVERT_RGB,
            1.0 if cam.convert_rgb else 0.0,
        )
    )
    # Smallest internal buffer = lowest latency; best-effort — DSHOW and
    # MSMF do not implement CAP_PROP_BUFFERSIZE at all, so this must never
    # feed a hard gate or every Windows calibrate/selftest would fail.
    reports.append(
        _set(cap, "buffersize", cv2.CAP_PROP_BUFFERSIZE, 1.0, informational=True)
    )
    return reports


def apply_settings(
    cap, settings: CameraSettings, quirks: BackendQuirks
) -> list[ControlReport]:
    """Apply persisted controls: auto-exposure first (manual exposure set
    under active AE gets clobbered), then exposure, gain, brightness."""
    reports: list[ControlReport] = []
    ae = (
        quirks.auto_exposure_on
        if settings.exposure_auto
        else quirks.auto_exposure_off
    )
    reports.append(_set(cap, "auto_exposure", cv2.CAP_PROP_AUTO_EXPOSURE, ae))
    if settings.exposure is not None:
        reports.append(
            _set(
                cap,
                "exposure",
                cv2.CAP_PROP_EXPOSURE,
                float(settings.exposure),
                quantize_tol=0.5 if quirks.exposure_is_log2 else None,
            )
        )
    if settings.gain is not None:
        reports.append(_set(cap, "gain", cv2.CAP_PROP_GAIN, float(settings.gain)))
    if settings.brightness is not None:
        reports.append(
            _set(
                cap,
                "brightness",
                cv2.CAP_PROP_BRIGHTNESS,
                float(settings.brightness),
            )
        )
    return reports


@dataclass(frozen=True, slots=True)
class FpsMeasurement:
    """Empirical frame-rate sample — never trust requested values."""

    fps: float
    frames: int
    read_failures: int
    elapsed_s: float


def measure_achieved_fps(
    cap,
    *,
    sample_s: float,
    warmup_frames: int = 5,
    clock: Callable[[], float] = time.monotonic,
) -> FpsMeasurement:
    """Read frames for ``sample_s`` (after a warmup the camera spends
    settling into the new mode) and report what the device actually
    delivers."""
    for _ in range(warmup_frames):
        cap.read()
    t0 = clock()
    frames = 0
    failures = 0
    while True:
        elapsed = clock() - t0
        if elapsed >= sample_s:
            break
        ok, _ = cap.read()
        if ok:
            frames += 1
        else:
            failures += 1
            time.sleep(0.005)  # no hot-spin on a wedged device
    elapsed = max(clock() - t0, 1e-9)
    return FpsMeasurement(
        fps=frames / elapsed,
        frames=frames,
        read_failures=failures,
        elapsed_s=elapsed,
    )


@dataclass(frozen=True, slots=True)
class ExposureEffectReport:
    """Did changing CAP_PROP_EXPOSURE actually change the image?"""

    baseline_mean: float
    stepped_mean: float
    delta: float
    ok: bool
    note: str = ""


def _mean_brightness(cap, frames: int) -> float:
    means = []
    for _ in range(frames):
        ok, img = cap.read()
        if ok and img is not None:
            means.append(float(np.asarray(img).mean()))
    return float(np.mean(means)) if means else float("nan")


def verify_exposure_effect(
    cap,
    exposure: float,
    *,
    step: float = 2.0,
    margin: float = 4.0,
    frames: int = 3,
    settle_frames: int = 2,
) -> ExposureEffectReport:
    """Step exposure by ``step``, expect mean frame brightness to move by
    more than ``margin`` counts, then restore the configured value.

    Perturbs the camera — calibrate/selftest only.
    """
    baseline = _mean_brightness(cap, frames)
    cap.set(cv2.CAP_PROP_EXPOSURE, float(exposure + step))
    for _ in range(settle_frames):
        cap.read()
    stepped = _mean_brightness(cap, frames)
    cap.set(cv2.CAP_PROP_EXPOSURE, float(exposure))
    for _ in range(settle_frames):
        cap.read()
    delta = stepped - baseline
    ok = abs(delta) >= margin
    note = "" if ok else (
        f"brightness moved {delta:+.1f} counts for a {step:+g} exposure step "
        f"(margin {margin:g}) — exposure control may have no effect"
    )
    return ExposureEffectReport(
        baseline_mean=baseline,
        stepped_mean=stepped,
        delta=delta,
        ok=ok,
        note=note,
    )
