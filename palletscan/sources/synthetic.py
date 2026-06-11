"""SyntheticSource: generated pallet passes with ground truth.

The whole scenario is planned at construction from a seeded RNG; per-pass
parameters come from child generators (``SeedSequence.spawn``), so pass *i*
is reproducible regardless of how frames are consumed, and the parameter
plan is independent of frame size — only compositing geometry changes.

Decodability is controlled by two dimensionless ratios (see config):
px/module and blur-in-modules. Pixel scale is derived per pass as
``px_per_meter = px_per_module / module_size_m``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from palletscan.config import SyntheticConfig
from palletscan.sources import render
from palletscan.sources.base import FrameSource
from palletscan.types import Frame, GroundTruthRecord, Symbology

MPH_TO_MPS = 0.44704
_BACKGROUND_VALUE = 96
_FACE_VALUE = 185


@dataclass(frozen=True, slots=True)
class _PassPlan:
    """Everything needed to composite one pass, planned up front."""

    pass_id: int
    payload: str
    symbology: Symbology
    idle_frames_before: int
    num_frames: int
    px_per_frame: float
    patch: np.ndarray  # degraded face patch (warped, blurred, contrast-scaled)
    alpha: np.ndarray  # float32 0..1 compositing weights (blurred warp mask)
    y_top: int
    pole_x: int | None  # static foreground occluder (pallet passes behind it)
    pole_w: int
    params: dict


class SyntheticSource(FrameSource):
    """Renders QR/Data Matrix pallet passes moving across the frame."""

    def __init__(
        self, cfg: SyntheticConfig, source_id: str = "synth0", tail_s: float = 1.0
    ) -> None:
        self._cfg = cfg
        self._source_id = source_id
        # Trailing idle after the last pass. The composition root sizes this
        # from downstream config (quiet frames + post-roll); the default only
        # suits direct/unit-test construction.
        self._tail_s = tail_s
        self.truth: list[GroundTruthRecord] = []
        master = np.random.SeedSequence(cfg.seed)
        children = master.spawn(cfg.num_passes + 2)
        scene_rng = np.random.Generator(np.random.PCG64(children[0]))
        self._noise_rng = np.random.Generator(np.random.PCG64(children[1]))
        # Static scene: frozen background texture and one lighting gradient
        # ("fairly constant ambient lighting" — spec §1).
        bg = np.full((cfg.height, cfg.width), _BACKGROUND_VALUE, np.float32)
        bg += scene_rng.normal(0.0, 2.0, bg.shape)
        gradient_dir = float(scene_rng.uniform(0.0, 360.0))
        gradient_amp = float(scene_rng.uniform(0.0, cfg.lighting_gradient_max))
        self._background = render.lighting_gradient(
            np.clip(bg, 0, 255).astype(np.uint8), gradient_amp, gradient_dir
        )
        self._plans = [
            self._plan_pass(i, np.random.Generator(np.random.PCG64(children[i + 2])))
            for i in range(cfg.num_passes)
        ]

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def nominal_fps(self) -> float:
        return self._cfg.fps

    @property
    def live(self) -> bool:
        # Realtime mode emulates a paced live camera; otherwise this is a
        # finite replay whose frames must all reach the pipeline.
        return self._cfg.realtime

    def _plan_pass(self, i: int, rng: np.random.Generator) -> _PassPlan:
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
        # Physical scale is derived from the px/module draw, so the
        # dimensionless envelope is independent of frame size.
        module_size_m = cfg.module_size_mm / 1000.0
        px_per_meter = sym.px_per_module / module_size_m
        speed_mps = speed_mph * MPH_TO_MPS
        px_per_frame = speed_mps * px_per_meter / cfg.fps
        exposure_s = cfg.exposure_fraction / cfg.fps
        blur_px = speed_mps * exposure_s * px_per_meter
        blur_modules = blur_px / sym.px_per_module

        # Pallet face: symbol (incl. quiet zone) on a larger flat panel.
        pad = max(8, round(0.3 * sym.image.shape[0]))
        face = np.full(
            (sym.image.shape[0] + 2 * pad, sym.image.shape[1] + 2 * pad),
            _FACE_VALUE,
            np.uint8,
        )
        face[pad : pad + sym.image.shape[0], pad : pad + sym.image.shape[1]] = (
            sym.image
        )
        face = render.apply_contrast(face, contrast)
        warped, mask = render.perspective_warp(face, angle_deg, background=0)
        # Constant speed during the pass: blur patch and mask once. The
        # blurred mask becomes a soft alpha edge, like a real moving object.
        patch = render.motion_blur(warped, blur_px)
        alpha = render.motion_blur(mask, blur_px).astype(np.float32) / 255.0

        max_jitter = max(1, (cfg.height - patch.shape[0]) // 4)
        y_center_off = int(rng.integers(-max_jitter, max_jitter + 1))
        y_top = int(
            np.clip(
                (cfg.height - patch.shape[0]) // 2 + y_center_off,
                0,
                max(0, cfg.height - patch.shape[0]),
            )
        )

        # Occlusion: a static occluder in the scene (post/pole) that the
        # pallet passes behind. Occlusion of the symbol is therefore
        # transient — some frames are damaged, others clean — unlike an
        # occluder attached to the pallet, which would make the whole pass
        # undecodable by construction.
        pole_x: int | None = None
        pole_w = 0
        if occlusion > 0:
            pole_w = max(1, round(occlusion * patch.shape[1]))
            pole_x = int(rng.uniform(0.3, 0.7) * cfg.width)

        travel_px = cfg.width + patch.shape[1]
        num_frames = max(2, int(np.ceil(travel_px / px_per_frame)))
        return _PassPlan(
            pass_id=i,
            payload=payload,
            symbology=symbology,
            idle_frames_before=max(1, round(idle_s * cfg.fps)),
            num_frames=num_frames,
            px_per_frame=px_per_frame,
            patch=patch,
            alpha=alpha,
            y_top=y_top,
            pole_x=pole_x,
            pole_w=pole_w,
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
            },
        )

    def _emit(self, image: np.ndarray, frame_index: int) -> Frame:
        if self._cfg.realtime:
            time.sleep(1.0 / self._cfg.fps)
        return Frame(
            image=image,
            ts=frame_index / self._cfg.fps,
            frame_index=frame_index,
            source_id=self._source_id,
        )

    @staticmethod
    def _draw_pole(frame: np.ndarray, plan: _PassPlan) -> None:
        if plan.pole_x is not None:
            frame[:, plan.pole_x : plan.pole_x + plan.pole_w] = 70

    def _idle_frame(self, plan: _PassPlan) -> np.ndarray:
        frame = self._background.copy()
        self._draw_pole(frame, plan)
        return render.add_noise(frame, plan.params["noise_sigma"], self._noise_rng)

    def frames(self) -> Iterator[Frame]:
        cfg = self._cfg
        idx = 0
        for j, plan in enumerate(self._plans):
            sigma = plan.params["noise_sigma"]
            # Idle gaps render with the *previous* pass's plan: the pole is
            # a static scene fixture, so it must not teleport mid-idle while
            # the prior segment is still counting quiet frames to close.
            idle_plan = self._plans[j - 1] if j > 0 else plan
            for _ in range(plan.idle_frames_before):
                yield self._emit(self._idle_frame(idle_plan), idx)
                idx += 1
            first_frame = idx
            ph, pw = plan.patch.shape
            for k in range(plan.num_frames):
                x = int(round(-pw + plan.px_per_frame * k))
                frame = self._background.copy()
                # Clip the patch to the frame on both axes (a face taller
                # than the frame would otherwise break the blend shapes).
                fx0, fx1 = max(0, x), min(cfg.width, x + pw)
                fy0 = max(0, plan.y_top)
                fy1 = min(cfg.height, plan.y_top + ph)
                if fx1 > fx0 and fy1 > fy0:
                    px0 = fx0 - x
                    py0 = fy0 - plan.y_top
                    region = frame[fy0:fy1, fx0:fx1].astype(np.float32)
                    a = plan.alpha[
                        py0 : py0 + (fy1 - fy0), px0 : px0 + (fx1 - fx0)
                    ]
                    p = plan.patch[
                        py0 : py0 + (fy1 - fy0), px0 : px0 + (fx1 - fx0)
                    ].astype(np.float32)
                    frame[fy0:fy1, fx0:fx1] = (
                        region * (1.0 - a) + p * a
                    ).astype(np.uint8)
                self._draw_pole(frame, plan)
                yield self._emit(render.add_noise(frame, sigma, self._noise_rng), idx)
                idx += 1
            self.truth.append(
                GroundTruthRecord(
                    pass_id=plan.pass_id,
                    payload=plan.payload,
                    symbology=plan.symbology,
                    first_frame=first_frame,
                    last_frame=idx - 1,
                    params=dict(plan.params),
                )
            )
        # Trailing idle so downstream segment close + post-roll can complete
        # before end-of-stream flush.
        if self._plans:
            for _ in range(round(self._tail_s * cfg.fps)):
                yield self._emit(self._idle_frame(self._plans[-1]), idx)
                idx += 1

    def write_truth_jsonl(self, path: Path | str) -> None:
        """Write accumulated ground truth (one JSON object per line)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for rec in self.truth:
                f.write(
                    json.dumps(
                        {
                            "pass_id": rec.pass_id,
                            "payload": rec.payload,
                            "symbology": rec.symbology.value,
                            "first_frame": rec.first_frame,
                            "last_frame": rec.last_frame,
                            **rec.params,
                        }
                    )
                    + "\n"
                )
