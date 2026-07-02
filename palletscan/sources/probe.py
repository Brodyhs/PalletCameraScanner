"""Empirical (format, resolution, fps) probing — never trust requested values.

UVC devices lie: a requested mode may be silently snapped to something
else, and the *achieved* frame rate routinely undershoots the negotiated
one. So calibration probes a candidate matrix with a **fresh capture per
candidate** (mode-switching a live handle is flaky on several backends),
reads back what the device actually landed on, and measures delivered
fps. ``choose_mode`` then ranks honestly: full resolution first, achieved
fps second, and uncompressed formats (Y8/UYVY/YUY2) over MJPG among
near-equals — accepting MJPG only when bandwidth makes it the only way to
sustain the rate (spec §2). The full table and the choice are logged.

Candidate matrices are suggestions to *try*, not assumptions — an
unsupported combination simply fails readback or measures low and loses
the ranking.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2

from palletscan.sources.controls import (
    fourcc_float,
    fourcc_str,
    measure_achieved_fps,
)

log = logging.getLogger(__name__)

#: Formats that arrive uncompressed (no MJPEG artifacts to cost decode rate).
UNCOMPRESSED_FOURCCS = frozenset({"GREY", "Y800", "Y8", "Y16", "UYVY", "YUY2"})

#: Achieved fps within this fraction of each other counts as "near-equal"
#: when applying the uncompressed-over-MJPG preference.
_NEAR_EQUAL_FPS = 0.05


@dataclass(frozen=True, slots=True)
class ModeCandidate:
    fourcc: str
    width: int
    height: int
    fps: float

    def describe(self) -> str:
        return f"{self.fourcc} {self.width}x{self.height}@{self.fps:g}"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    candidate: ModeCandidate
    opened: bool
    actual_fourcc: str | None = None
    actual_width: int | None = None
    actual_height: int | None = None
    achieved_fps: float | None = None
    frames_sampled: int = 0
    error: str | None = None

    @property
    def actual_area(self) -> int:
        if self.actual_width is None or self.actual_height is None:
            return 0
        return self.actual_width * self.actual_height


def candidates_for(
    device_name: str, current: ModeCandidate | None = None
) -> list[ModeCandidate]:
    """Candidate matrix for a device name (pure; ``current`` is the mode the
    caller read off the device, tried first on unknown hardware)."""
    name = device_name.lower()
    if "see3cam_24cug" in name:
        return [
            ModeCandidate(fourcc, 1920, 1200, fps)
            for fps in (120.0, 60.0, 30.0)
            for fourcc in ("UYVY", "YUY2", "MJPG")
        ]
    if "see3cam_37cugm" in name:
        return [
            ModeCandidate(fourcc, 2064, 1552, fps)
            for fps in (72.0, 60.0, 30.0)
            for fourcc in ("GREY", "UYVY", "MJPG")
        ]
    generic = [
        ModeCandidate(fourcc, w, h, fps)
        for (w, h) in ((1920, 1080), (1280, 720), (640, 480))
        for fps in (60.0, 30.0)
        for fourcc in ("YUY2", "MJPG")
    ]
    if current is not None and current not in generic:
        generic.insert(0, current)
    return generic


def current_mode(cap) -> ModeCandidate:
    """The mode a device reports right now (the generic-device seed)."""
    return ModeCandidate(
        fourcc=fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)).strip(),
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        fps=float(cap.get(cv2.CAP_PROP_FPS)),
    )


def probe_modes(
    make_cap: Callable[[ModeCandidate], object],
    candidates: list[ModeCandidate],
    *,
    sample_s: float = 1.0,
    warmup_frames: int = 5,
    clock: Callable[[], float] = time.monotonic,
) -> list[ProbeResult]:
    """Try every candidate on a fresh capture and report what really happened.

    ``make_cap`` receives the candidate being probed: backends whose format
    is fixed at capture construction (pygrabber's DirectShow graph — its
    mode ``set()`` calls are accepted no-ops) must build each candidate
    into its own capture, or every candidate is measured on the same
    seed-negotiated format (re-review of REVIEW bringup-4d95b67). cv2
    backends may ignore it; the ``set()`` calls below program them."""
    results: list[ProbeResult] = []
    for cand in candidates:
        cap = make_cap(cand)
        try:
            if not cap.isOpened():  # type: ignore[attr-defined]
                results.append(
                    ProbeResult(cand, opened=False, error="could not open device")
                )
                continue
            cap.set(cv2.CAP_PROP_FOURCC, fourcc_float(cand.fourcc))  # type: ignore[attr-defined]
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(cand.width))  # type: ignore[attr-defined]
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(cand.height))  # type: ignore[attr-defined]
            cap.set(cv2.CAP_PROP_FPS, float(cand.fps))  # type: ignore[attr-defined]
            actual = current_mode(cap)
            m = measure_achieved_fps(
                cap, sample_s=sample_s, warmup_frames=warmup_frames, clock=clock
            )
            results.append(
                ProbeResult(
                    cand,
                    opened=True,
                    actual_fourcc=actual.fourcc,
                    actual_width=actual.width,
                    actual_height=actual.height,
                    achieved_fps=m.fps,
                    frames_sampled=m.frames,
                )
            )
        except Exception as exc:
            results.append(ProbeResult(cand, opened=False, error=repr(exc)))
        finally:
            try:
                cap.release()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - defensive
                log.warning("release failed after probing %s", cand.describe())
    return results


def choose_mode(
    results: list[ProbeResult], *, min_fps_fraction: float = 0.9
) -> ProbeResult | None:
    """Pick the best probed mode (pure, cv2-free).

    Qualify: opened, measured, and achieved >= ``min_fps_fraction`` of the
    *requested* fps. Rank: actual resolution area desc, then achieved fps
    desc. Among near-equals (within 5% fps at the top area), prefer
    uncompressed over MJPG.
    """
    viable = [
        r
        for r in results
        if r.opened
        and r.achieved_fps is not None
        and r.achieved_fps >= min_fps_fraction * r.candidate.fps
        and r.actual_area > 0
    ]
    if not viable:
        return None
    top_area = max(r.actual_area for r in viable)
    finalists = [r for r in viable if r.actual_area == top_area]
    best_fps = max(r.achieved_fps for r in finalists)  # type: ignore[type-var]
    near = [
        r
        for r in finalists
        if r.achieved_fps >= (1.0 - _NEAR_EQUAL_FPS) * best_fps  # type: ignore[operator]
    ]
    uncompressed = [
        r
        for r in near
        if (r.actual_fourcc or "").strip().upper() in UNCOMPRESSED_FOURCCS
    ]
    pool = uncompressed or near
    return max(pool, key=lambda r: r.achieved_fps or 0.0)


def format_probe_table(
    results: list[ProbeResult], chosen: ProbeResult | None = None
) -> str:
    """Human-readable probe table (calibrate console + structured log)."""
    lines = [
        f"{'requested':<24} {'actual':<22} {'achieved fps':>12}  note",
    ]
    for r in results:
        if not r.opened:
            actual, fps_s, note = "-", "-", r.error or "did not open"
        else:
            actual = (
                f"{r.actual_fourcc} {r.actual_width}x{r.actual_height}"
            )
            fps_s = f"{r.achieved_fps:.1f}" if r.achieved_fps is not None else "-"
            note = "<= CHOSEN" if (chosen is not None and r is chosen) else ""
        lines.append(
            f"{r.candidate.describe():<24} {actual:<22} {fps_s:>12}  {note}"
        )
    return "\n".join(lines)
