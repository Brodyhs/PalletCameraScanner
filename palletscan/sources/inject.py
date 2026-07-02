"""CameraInjectionSource: physics-degraded synthetic pallets composited onto a
LIVE camera feed, fed through the real pipeline with ground-truth reconciliation.

It SUBCLASSES :class:`SyntheticSource` for two reasons: (1) the pass-planning /
seeding / ``.truth`` / ``write_truth_jsonl`` machinery is inherited, and (2)
``PipelineRunner``'s ``isinstance(source, SyntheticSource)`` gate then auto-builds
the read-rate / miss report off ``self.truth``. It is a DECORATOR around the real
watchdog-wrapped camera (:func:`build_camera_source`): idle gaps pass the live
frame through untouched (the real scene IS the background), and during a pass one
degraded moving code is alpha-blended onto each live frame.

HONESTY: an injected code never travels through the lens or sensor, so this
certifies the DECODE + PIPELINE + motion-gate-vs-real-background under a *physics
model* of the optics (motion blur from the live exposure, px/module from the
simulated distance) — NOT the literal optical capture. Pair it with
record-then-replay (VideoFileSource) to certify the real lens.

TRUTH TIME-BASE: ``self.truth`` records ``first_frame``/``last_frame`` as
``round(frame.ts * nominal_fps)`` — nominal-fps *ticks of the live source
clock* — NOT live camera frame indices. ``reconcile_truth(truth, events,
fps=source.nominal_fps)`` divides by fps to rebuild a time window, and live
Pass/MissEvent timestamps are source-clock ``ts``; frame indices diverge from
ts under stalls/outages/fps error, so index-based truth made every genuinely
missed pass "unaccounted". For SyntheticSource ``ts == frame_index / fps``,
so this tick contract collapses to the inherited frame-index one. Each
record's ``params`` also carries the raw ``ts_first``/``ts_last`` and
``truncated=True`` when a watchdog reconnect tore the pass mid-flight (its
frames were already delivered, so it must still reconcile, never vanish).
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np
from pydantic import ValidationError

from palletscan.config import AppConfig, SyntheticConfig, resolve_camera
from palletscan.sources import render
from palletscan.sources.base import FrameSource
from palletscan.sources.camera import build_camera_source
from palletscan.sources.synthetic import (
    _FACE_VALUE,
    MPH_TO_MPS,
    SyntheticSource,
    _PassPlan,
)
from palletscan.types import Frame, GroundTruthRecord, Symbology

_UNIT = {
    "right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0),
    "downright": (1.0, 1.0), "downleft": (-1.0, 1.0),
    "upright": (1.0, -1.0), "upleft": (-1.0, -1.0),
}


def _trajectory(
    direction: str, w: int, h: int, pw: int, ph: int, step: float,
    rng: np.random.Generator,
) -> tuple[float, float, float, float, int]:
    """Top-left start (x0,y0), per-frame velocity (vx,vy) and frame count for a
    (pw x ph) patch crossing a w x h frame in ``direction`` at ``step`` px/frame:
    enters fully off the entry edge, exits fully off the far edge. Movement axes
    start off-screen; a non-moving axis is jittered around the centre line."""
    ux, uy = _UNIT.get(direction, (1.0, 0.0))
    n = math.hypot(ux, uy) or 1.0
    vx, vy = ux / n * step, uy / n * step

    def _start(v: float, extent: int, patch: int) -> float:
        if v > 0:
            return float(-patch)
        if v < 0:
            return float(extent)
        j = max(1, (extent - patch) // 4)
        return float(np.clip((extent - patch) // 2 + int(rng.integers(-j, j + 1)),
                             0, max(0, extent - patch)))

    x0, y0 = _start(vx, w, pw), _start(vy, h, ph)
    # The patch is fully gone as soon as EITHER moving axis has completely
    # exited the frame (motion is monotonic), so the pass length is the MIN
    # of the per-axis crossing times — max would keep a diagonal pass
    # "active" for many frames while compositing nothing.
    times: list[float] = []
    if vx != 0.0:
        times.append((w + pw) / abs(vx))
    if vy != 0.0:
        times.append((h + ph) / abs(vy))
    nf = max(2, int(math.ceil(min(times)))) if times else 2
    return x0, y0, vx, vy, nf


class CameraInjectionSource(SyntheticSource):
    def __init__(
        self,
        cfg: SyntheticConfig,
        app_cfg: AppConfig,
        *,
        source_id: str = "inject0",
        exposure_s: float = 0.001,
        inner: FrameSource | None = None,
    ) -> None:
        cam = resolve_camera(app_cfg)
        # Match the plan geometry to the live camera frame and tag injected
        # payloads distinctly from the operator's real codes (INJ-...).
        # model_validate, not model_copy: model_copy(update=...) skips
        # validation, so a camera config that legitimately omits its locked
        # mode would smuggle None into required plan fields and crash later
        # with an opaque TypeError deep in rendering.
        try:
            cfg = SyntheticConfig.model_validate(
                {
                    **cfg.model_dump(),
                    "width": cam.width,
                    "height": cam.height,
                    "fps": cam.fps,
                    "payload_prefix": "INJ-",
                }
            )
        except ValidationError as exc:
            missing = [
                f for f in ("width", "height", "fps") if getattr(cam, f) is None
            ]
            if not missing:
                raise
            raise ValueError(
                f"cannot inject onto camera {cam.id!r}: cameras[]."
                f"{'/'.join(missing)} not set — injection plans trajectories "
                "in the locked mode's pixel/frame space; run `palletscan "
                "calibrate --save` or set width/height/fps explicitly"
            ) from exc
        super().__init__(cfg, source_id=source_id)
        self._exposure_s = float(exposure_s)
        # Discard the synthetic static background (never used); the live frame
        # is the background.
        self._background = None  # type: ignore[assignment]
        self._inner: FrameSource = (
            inner if inner is not None else build_camera_source(app_cfg)
        )

    @property
    def live(self) -> bool:
        return True

    @property
    def nominal_fps(self) -> float:
        return self._inner.nominal_fps or self._cfg.fps

    def close(self) -> None:
        self._inner.close()

    def _plan_pass(self, i: int, rng: np.random.Generator) -> _PassPlan:
        # Verbatim SyntheticSource._plan_pass EXCEPT the exposure that drives the
        # motion blur is the live/owned camera exposure, not a synthetic fraction.
        cfg = self._cfg
        speed_mph = float(rng.uniform(*cfg.speed_mph_range))
        angle_deg = float(rng.uniform(*cfg.angle_deg_range))
        px_per_module = float(rng.uniform(*cfg.px_per_module_range))
        contrast = float(rng.uniform(*cfg.contrast_range))
        noise_sigma = float(rng.uniform(*cfg.noise_sigma_range))
        occlusion = float(rng.uniform(0.0, cfg.occlusion_max_frac))
        idle_s = float(rng.uniform(*cfg.idle_s_range))
        symbology = cfg.symbologies[i % len(cfg.symbologies)]
        payload = f"{cfg.payload_prefix}{i + 1:06d}"

        sym = (
            render.render_qr(payload, px_per_module)
            if symbology is Symbology.QR
            else render.render_datamatrix(payload, px_per_module)
        )
        module_size_m = cfg.module_size_mm / 1000.0
        px_per_meter = sym.px_per_module / module_size_m
        speed_mps = speed_mph * MPH_TO_MPS
        px_per_frame = speed_mps * px_per_meter / cfg.fps
        exposure_s = self._exposure_s  # <-- the only change vs SyntheticSource
        blur_px = speed_mps * exposure_s * px_per_meter
        blur_modules = blur_px / sym.px_per_module

        pad = max(8, round(0.3 * sym.image.shape[0]))
        face = np.full(
            (sym.image.shape[0] + 2 * pad, sym.image.shape[1] + 2 * pad),
            _FACE_VALUE,
            np.uint8,
        )
        face[pad : pad + sym.image.shape[0], pad : pad + sym.image.shape[1]] = sym.image
        face = render.apply_contrast(face, contrast)
        warped, mask = render.perspective_warp(face, angle_deg, background=0)
        patch = render.motion_blur(warped, blur_px)
        alpha = render.motion_blur(mask, blur_px).astype(np.float32) / 255.0

        ph, pw = patch.shape
        direction = str(rng.choice(cfg.directions)) if cfg.directions else "right"
        x0, y0, vx, vy, num_frames = _trajectory(
            direction, cfg.width, cfg.height, pw, ph, px_per_frame, rng
        )
        return _PassPlan(
            pass_id=i,
            payload=payload,
            symbology=symbology,
            idle_frames_before=max(1, round(idle_s * cfg.fps)),
            num_frames=max(2, num_frames),
            px_per_frame=px_per_frame,
            patch=patch,
            alpha=alpha,
            y_top=int(y0),
            pole_x=None,
            pole_w=0,
            params={
                "speed_mph": speed_mph,
                "angle_deg": angle_deg,
                "px_per_module": sym.px_per_module,
                "blur_px": blur_px,
                "blur_modules": blur_modules,
                "contrast": contrast,
                "noise_sigma": noise_sigma,
                "occlusion_frac": occlusion,
                "code_px": sym.modules * sym.px_per_module,
                "modules": sym.modules,
                "idle_s_before": idle_s,
                "exposure_s": exposure_s,
                "direction": direction,
            },
            x0=x0, y0=y0, vx=vx, vy=vy,
        )

    def _composite(self, base: np.ndarray, plan: _PassPlan, k: int) -> np.ndarray:
        """Alpha-blend the degraded code at step k onto a copy of the live frame
        (the synthetic inner block, but onto real pixels instead of a fake bg)."""
        img = base.copy()
        ph, pw = plan.patch.shape
        h, w = img.shape
        x = int(round((plan.x0 if plan.x0 is not None else -pw) + plan.vx * k))
        y = int(round((plan.y0 if plan.y0 is not None else plan.y_top) + plan.vy * k))
        fx0, fx1 = max(0, x), min(w, x + pw)
        fy0, fy1 = max(0, y), min(h, y + ph)
        if fx1 > fx0 and fy1 > fy0:
            px0, py0 = fx0 - x, fy0 - y
            region = img[fy0:fy1, fx0:fx1].astype(np.float32)
            a = plan.alpha[py0 : py0 + (fy1 - fy0), px0 : px0 + (fx1 - fx0)]
            p = plan.patch[py0 : py0 + (fy1 - fy0), px0 : px0 + (fx1 - fx0)].astype(
                np.float32
            )
            img[fy0:fy1, fx0:fx1] = (region * (1.0 - a) + p * a).astype(np.uint8)
        return img

    def _retire(
        self, entry: dict, *, last_ts: float, fps: float, truncated: bool = False
    ) -> None:
        """Finalize one in-flight pass into ``self.truth``.

        Frame bounds are nominal-fps ticks of the live ts clock (module
        docstring: TRUTH TIME-BASE) so ``reconcile_truth`` maps them back
        onto the same axis Pass/MissEvent timestamps use."""
        plan: _PassPlan = entry["plan"]
        params = dict(plan.params)
        params["ts_first"] = entry["ts_first"]
        params["ts_last"] = last_ts
        if truncated:
            params["truncated"] = True
        self.truth.append(
            GroundTruthRecord(
                pass_id=plan.pass_id,
                payload=plan.payload,
                symbology=plan.symbology,
                first_frame=round(entry["ts_first"] * fps),
                last_frame=round(last_ts * fps),
                params=params,
            )
        )

    def frames(self) -> Iterator[Frame]:
        # Maintain up to ``max_concurrent`` staggered passes at once and chain
        # their composites onto each live frame, so the demo shows MULTIPLE moving
        # codes simultaneously (decode + preview already render a box per code).
        # max_concurrent=1 reproduces the original one-at-a-time behavior.
        cfg = self._cfg
        fps = self.nominal_fps
        max_active = max(1, getattr(cfg, "max_concurrent", 1))
        active: list[dict] = []  # each: {"plan", "k", "ts_first"}
        next_j = 0
        # Cache pass 0's plan: planning renders the full degraded patch, so a
        # throwaway _plan(0) just to read idle_frames_before would render
        # pass 0 twice (once discarded).
        plan0: _PassPlan | None = self._plan(0) if cfg.num_passes > 0 else None
        launch_in = plan0.idle_frames_before if plan0 is not None else 0
        tail_remaining = int(round(self._tail_s * cfg.fps))
        prev_ts: float | None = None
        for fr in self._inner.frames():
            base = fr.image
            if fr.discontinuity and active:
                # A reconnect tears every in-flight code — but its frames were
                # already composited and delivered, so finalize it into truth
                # (flagged truncated) instead of silently vanishing: the
                # account-for-everything reconcile must still see it.
                last = prev_ts if prev_ts is not None else fr.ts
                for e in active:
                    self._retire(e, last_ts=last, fps=fps, truncated=True)
                active = []
            # Launch new staggered passes up to the concurrency cap. The idle
            # countdown runs ONLY while a launch slot is open: a pass that
            # outlives its idle window must still be followed by a full idle
            # gap (the idle_frames_before contract) — an always-running
            # countdown goes deeply negative and launches back-to-back.
            if next_j < cfg.num_passes and len(active) < max_active:
                launch_in -= 1
                if launch_in <= 0:
                    plan = plan0 if plan0 is not None else self._plan(next_j)
                    plan0 = None  # release the cached patch after pass 0
                    active.append({"plan": plan, "k": 0, "ts_first": fr.ts})
                    next_j += 1
                    launch_in = max(1, plan.idle_frames_before)
            # Composite every active code onto this frame (chained alpha blends).
            out = base
            for e in active:
                out = self._composite(out, e["plan"], e["k"])
                e["k"] += 1
            # Retire finished passes, recording each one's own ground truth.
            still: list[dict] = []
            for e in active:
                if e["k"] >= e["plan"].num_frames:
                    self._retire(e, last_ts=fr.ts, fps=fps)
                else:
                    still.append(e)
            active = still
            prev_ts = fr.ts
            if next_j >= cfg.num_passes and not active:  # all launched + retired
                tail_remaining -= 1
                if tail_remaining <= 0:
                    yield Frame(
                        image=out, ts=fr.ts, frame_index=fr.frame_index,
                        source_id=fr.source_id, discontinuity=fr.discontinuity,
                    )
                    return
            yield Frame(
                image=out, ts=fr.ts, frame_index=fr.frame_index,
                source_id=fr.source_id, discontinuity=fr.discontinuity,
            )
