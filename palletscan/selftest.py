"""Startup self-test: refuse to run blind (spec §5).

Three check groups, all honest about severity (``hard`` failures exit 1;
soft ones print WARN and continue):

1. **Cameras** — runs iff ``cameras:`` is configured (skipped with a
   notice when empty so a pre-hardware station still selftests; or via
   ``--skip-camera``): every entry must enumerate by name, open, take its
   mode and settings (readback hard-fails on ``controls_reliable``
   backends, warns on AVFoundation), and deliver ≥ 0.85× the configured
   fps measured over ~2 s.
2. **Decode through the full pipeline** — the bundled assets (generated
   by ``tools/make_selftest_assets.py`` and committed; no runtime
   generation) are swept across a synthetic frame by an in-module
   FrameSource and pushed through ``PipelineRunner`` with outputs rebased
   to a scratch directory: exactly the expected pass events, zero misses.
   This exercises MotionGate + DecodeEngine + PassTracker + bus +
   evidence wiring, not just the decoder libraries.
3. **Disk space** — free space on the evidence and outbox volumes must
   exceed 2× the configured caps (hard) and warns under 4×.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from palletscan.config import AppConfig, apply_overrides
from palletscan.sources.base import FrameSource
from palletscan.sources.camera import (
    CaptureFactory,
    DeviceLister,
    default_capture_factory,
)
from palletscan.sources.controls import (
    apply_mode,
    apply_settings,
    measure_achieved_fps,
    quirks_for,
    verify_exposure_effect,
)
from palletscan.sources.devices import backend_flag, find_device, list_devices
from palletscan.types import Frame, MissEvent, PassEvent, Symbology

log = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).parent / "assets"

#: Bundled known-good symbols (payload -> file), per symbology.
SELFTEST_ASSETS: dict[Symbology, tuple[str, Path]] = {
    Symbology.QR: ("PALLETSCAN-SELFTEST-QR", _ASSETS_DIR / "selftest_qr.png"),
    Symbology.DATAMATRIX: (
        "PALLETSCAN-SELFTEST-DM",
        _ASSETS_DIR / "selftest_dm.png",
    ),
}

_FPS_HARD_FRACTION = 0.85
_FPS_SAMPLE_S = 2.0
_DISK_HARD_FACTOR = 2.0
_DISK_WARN_FACTOR = 4.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    hard: bool = True  # ok=False+hard=False renders as WARN, not FAIL

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL" if self.hard else "WARN"


@dataclass(slots=True)
class SelftestReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.hard)

    def format(self) -> str:
        lines = ["── selftest ──"]
        for c in self.checks:
            lines.append(f"[{c.status}] {c.name}: {c.detail}")
        lines.append("selftest: " + ("OK" if self.ok else "FAILED (refusing to run blind)"))
        return "\n".join(lines)


class _AssetSweepSource(FrameSource):
    """Sweeps each asset across a gray background: idle, pass, idle — so a
    motion segment opens, decodes, and closes for every symbol."""

    def __init__(
        self,
        images: list[np.ndarray],
        *,
        fps: float = 30.0,
        idle_frames: int = 12,
        sweep_frames: int = 40,
    ) -> None:
        self._images = images
        self._fps = fps
        self._idle = idle_frames
        self._sweep = sweep_frames
        self._h = max(int(im.shape[0]) for im in images) + 120
        self._w = max(int(im.shape[1]) for im in images) * 2 + 240
        self._closed = False

    @property
    def source_id(self) -> str:
        return "selftest"

    @property
    def nominal_fps(self) -> float:
        return self._fps

    def frames(self) -> Iterator[Frame]:
        idx = 0
        for image in self._images:
            h, w = image.shape[:2]
            y = (self._h - h) // 2
            travel = self._w - w - 40
            for j in range(self._idle + self._sweep + self._idle):
                if self._closed:
                    return
                frame = np.full((self._h, self._w), 128, np.uint8)
                k = j - self._idle
                if 0 <= k < self._sweep:
                    x = 20 + (travel * k) // (self._sweep - 1)
                    frame[y : y + h, x : x + w] = image
                yield Frame(
                    image=frame, ts=idx / self._fps, frame_index=idx,
                    source_id=self.source_id,
                )
                idx += 1

    def close(self) -> None:
        self._closed = True


def _disk_label(path: Path) -> Path:
    """Nearest existing ancestor (the dirs may not exist before first run)."""
    p = path if path.is_absolute() else Path.cwd() / path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _check_cameras(
    cfg: AppConfig,
    capture_factory: CaptureFactory,
    device_lister: DeviceLister,
    clock: Callable[[], float],
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    try:
        devices = device_lister()
    except Exception as exc:  # pragma: no cover - lister contract returns []
        return [CheckResult("cameras/enumeration", False, repr(exc))]
    for cam in cfg.cameras:
        name = f"camera[{cam.id}]"
        try:
            if devices:
                dev = find_device(devices, cam.name)
                index, flag = dev.index, (
                    dev.backend if cam.backend.value == "auto" else backend_flag(cam.backend)
                )
            elif cam.fallback_index is not None:
                index, flag = cam.fallback_index, backend_flag(cam.backend)
            else:
                raise ValueError(
                    f"no devices enumerated and no fallback_index "
                    f"(looking for {cam.name!r})"
                )
        except ValueError as exc:
            checks.append(CheckResult(f"{name}/resolve", False, str(exc)))
            continue
        cap = capture_factory(index, flag)
        try:
            if not cap.isOpened():
                checks.append(
                    CheckResult(
                        f"{name}/open", False, f"index {index} did not open"
                    )
                )
                continue
            checks.append(
                CheckResult(f"{name}/resolve", True, f"{cam.name!r} -> index {index}")
            )
            quirks = quirks_for(cam.backend)
            reports = apply_mode(cap, cam) + apply_settings(
                cap, cam.settings, quirks
            )
            bad = [
                r.prop for r in reports if not (r.verified or r.informational)
            ]
            checks.append(
                CheckResult(
                    f"{name}/controls",
                    not bad,
                    "all controls verified" if not bad else f"unverified: {bad}",
                    hard=quirks.controls_reliable,
                )
            )
            if not cam.settings.exposure_auto and cam.settings.exposure is not None:
                # Perturbs (then restores) the camera — allowed here, never
                # on the run path. Hard only where controls are reliable.
                effect = verify_exposure_effect(cap, cam.settings.exposure)
                checks.append(
                    CheckResult(
                        f"{name}/exposure_effect",
                        effect.ok,
                        f"brightness {effect.baseline_mean:.1f} -> "
                        f"{effect.stepped_mean:.1f} (delta {effect.delta:+.1f})"
                        + ("" if effect.ok else f"; {effect.note}"),
                        hard=quirks.controls_reliable,
                    )
                )
            m = measure_achieved_fps(cap, sample_s=_FPS_SAMPLE_S, clock=clock)
            if cam.fps is not None:
                floor = _FPS_HARD_FRACTION * cam.fps
                checks.append(
                    CheckResult(
                        f"{name}/fps",
                        m.fps >= floor,
                        f"achieved {m.fps:.1f} fps vs configured {cam.fps:g} "
                        f"(hard floor {floor:.1f})",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        f"{name}/fps",
                        True,
                        f"achieved {m.fps:.1f} fps (no configured fps to gate on)",
                        hard=False,
                    )
                )
        finally:
            cap.release()
    return checks


def _check_pipeline_decode(
    cfg: AppConfig,
    data_dir: Path | str | None,
    assets: dict[Symbology, tuple[str, Path]],
) -> CheckResult:
    from palletscan.app import PipelineRunner  # lazy: heavy import chain

    selected = [s for s in cfg.decode.symbology_priority if s in assets]
    if not selected:
        return CheckResult(
            "pipeline/decode", False, "no selftest asset for the configured "
            f"symbology_priority {cfg.decode.symbology_priority}"
        )
    images: list[np.ndarray] = []
    expected: set[str] = set()
    for sym in selected:
        payload, path = assets[sym]
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return CheckResult(
                "pipeline/decode", False, f"bundled asset missing: {path}"
            )
        images.append(img)
        expected.add(payload)
    scratch = (
        Path(data_dir)
        if data_dir is not None
        else Path(tempfile.mkdtemp(prefix="palletscan-selftest-"))
    )
    run_cfg = apply_overrides(cfg, data_dir=scratch)
    source = _AssetSweepSource(images)
    runner = PipelineRunner(run_cfg, source, sinks=[])  # no configured sinks
    summary = runner.run()
    pass_events = [
        e for e in runner.collected_events if isinstance(e, PassEvent)
    ]
    misses = [e for e in runner.collected_events if isinstance(e, MissEvent)]
    payloads = {e.payload for e in pass_events}
    # Exactly the expected events: a duplicate or unexpected pass is as
    # much a pipeline defect here as a missing one.
    ok = (
        payloads == expected
        and len(pass_events) == len(expected)
        and not misses
    )
    return CheckResult(
        "pipeline/decode",
        ok,
        f"{len(pass_events)} pass event(s) {sorted(payloads)} of expected "
        f"{sorted(expected)}, {len(misses)} miss(es), {summary.frames} frames",
    )


def _check_disk(
    cfg: AppConfig, disk_usage: Callable[[Path], object]
) -> list[CheckResult]:
    checks = []
    need_mb = cfg.evidence.max_total_mb + cfg.sinks.http.max_mb
    for label, path in (
        ("evidence", Path(cfg.evidence.dir)),
        ("outbox", Path(cfg.sinks.http.outbox_path).parent),
    ):
        probe = _disk_label(path)
        free_mb = disk_usage(probe).free / (1024 * 1024)  # type: ignore[attr-defined]
        if free_mb < _DISK_HARD_FACTOR * need_mb:
            checks.append(
                CheckResult(
                    f"disk/{label}",
                    False,
                    f"{free_mb:.0f} MB free on {probe} < "
                    f"{_DISK_HARD_FACTOR:g}x the {need_mb:.0f} MB caps",
                )
            )
        elif free_mb < _DISK_WARN_FACTOR * need_mb:
            checks.append(
                CheckResult(
                    f"disk/{label}",
                    False,
                    f"{free_mb:.0f} MB free on {probe} is under "
                    f"{_DISK_WARN_FACTOR:g}x the {need_mb:.0f} MB caps",
                    hard=False,
                )
            )
        else:
            checks.append(
                CheckResult(f"disk/{label}", True, f"{free_mb:.0f} MB free on {probe}")
            )
    return checks


def run_selftest(
    cfg: AppConfig,
    *,
    capture_factory: CaptureFactory = default_capture_factory,
    device_lister: DeviceLister = list_devices,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    clock: Callable[[], float] = time.monotonic,
    data_dir: Path | str | None = None,
    skip_camera: bool = False,
    assets: dict[Symbology, tuple[str, Path]] | None = None,
) -> SelftestReport:
    """Run all checks; every injectable has a production default."""
    report = SelftestReport()
    if skip_camera:
        report.checks.append(
            CheckResult("cameras", True, "skipped (--skip-camera)", hard=False)
        )
    elif not cfg.cameras:
        report.checks.append(
            CheckResult(
                "cameras",
                True,
                "skipped: no cameras configured (pre-hardware selftest)",
                hard=False,
            )
        )
    else:
        report.checks.extend(
            _check_cameras(cfg, capture_factory, device_lister, clock)
        )
    try:
        report.checks.append(
            _check_pipeline_decode(cfg, data_dir, assets or SELFTEST_ASSETS)
        )
    except Exception as exc:
        log.exception("selftest pipeline decode crashed")
        report.checks.append(CheckResult("pipeline/decode", False, repr(exc)))
    report.checks.extend(_check_disk(cfg, disk_usage))
    return report
