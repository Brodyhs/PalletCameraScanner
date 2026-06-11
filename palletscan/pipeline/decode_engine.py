"""DecodeEngine: budget-aware decode cascade over an Executor.

Cascade per frame, on the motion ROI crop only:

1. Fast path: pyzbar (QR).
2. pylibdmtx (Data Matrix) with its native timeout capped by the remaining
   frame budget — never on a full frame.
3. Preprocessing variants (only while the pass remains undecoded after
   ``fallback_after_frames`` attempts), fanned out on the executor; first
   hit wins.

The frame budget is a *soft* deadline: an in-flight C call cannot be
cancelled, so worst-case overshoot is bounded by ROI size and libdmtx's own
timeout; overshoots are counted in :attr:`budget_overruns`.

Early-exit happens one level up: once a pass is confirmed, the pipeline
skips ``decode_frame`` entirely for its remaining frames.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Executor, wait
from dataclasses import dataclass, field

import numpy as np

from palletscan.config import DecodeConfig
from palletscan.pipeline import preprocess
from palletscan.pipeline.decoders import (
    PylibdmtxDecoder,
    PyzbarDecoder,
    RawDecode,
)
from palletscan.types import DecodeResult, Frame, Roi, Symbology


@dataclass(slots=True)
class PassDecodeContext:
    """Per-segment decode state, owned by the PassTracker."""

    confirmed: bool = False
    frames_attempted: int = 0
    fallback_runs: int = 0


@dataclass(slots=True)
class _Counters:
    pyzbar_calls: int = 0
    dmtx_calls: int = 0
    fallback_calls: int = 0
    budget_overruns: int = 0


def _variant_task(
    variant_name: str,
    crop: np.ndarray,
    symbologies: tuple[Symbology, ...],
    dm_timeout_ms: int,
) -> tuple[str, str, list[RawDecode]]:
    """Run one preprocessing variant + decoders. Top-level for picklability
    (process executor support)."""
    fn = dict(preprocess.VARIANTS)[variant_name]
    processed = fn(crop)
    for sym in symbologies:
        if sym is Symbology.QR:
            hits = PyzbarDecoder().decode(processed)
            if hits:
                return variant_name, f"pyzbar+{variant_name}", hits
        elif sym is Symbology.DATAMATRIX:
            hits = PylibdmtxDecoder().decode(processed, dm_timeout_ms)
            if hits:
                return variant_name, f"pylibdmtx+{variant_name}", hits
    return variant_name, "", []


class DecodeEngine:
    """Stateless apart from instrumentation counters; one per pipeline."""

    def __init__(self, cfg: DecodeConfig, executor: Executor) -> None:
        self._cfg = cfg
        self._executor = executor
        self._pyzbar = PyzbarDecoder()
        self._dmtx = PylibdmtxDecoder()
        self.counters = _Counters()

    def _results(
        self,
        raw: list[RawDecode],
        frame: Frame,
        crop_origin: tuple[int, int],
        decoder: str,
        started: float,
    ) -> list[DecodeResult]:
        ox, oy = crop_origin
        latency_ms = (time.perf_counter() - started) * 1000.0
        return [
            DecodeResult(
                payload=r.payload,
                symbology=r.symbology,
                roi=Roi(r.roi.x + ox, r.roi.y + oy, r.roi.w, r.roi.h),
                frame_index=frame.frame_index,
                ts=frame.ts,
                source_id=frame.source_id,
                decoder=decoder,
                latency_ms=latency_ms,
            )
            for r in raw
        ]

    def decode_frame(
        self, frame: Frame, roi: Roi, ctx: PassDecodeContext
    ) -> list[DecodeResult]:
        """Run the cascade on one frame's ROI. Returns [] if nothing decoded."""
        if ctx.confirmed:
            return []
        cfg = self._cfg
        started = time.perf_counter()
        deadline = started + cfg.frame_budget_ms / 1000.0
        crop = roi.clamp(frame.image.shape).crop(frame.image)
        origin = (roi.clamp(frame.image.shape).x, roi.clamp(frame.image.shape).y)

        try:
            # Steps 1+2: plain decoders in priority order.
            for sym in cfg.symbology_priority:
                if sym is Symbology.QR:
                    self.counters.pyzbar_calls += 1
                    hits = self._pyzbar.decode(crop)
                    if hits:
                        return self._results(hits, frame, origin, "pyzbar", started)
                elif sym is Symbology.DATAMATRIX:
                    remaining_ms = (deadline - time.perf_counter()) * 1000.0
                    if remaining_ms <= 1:
                        break
                    self.counters.dmtx_calls += 1
                    hits = self._dmtx.decode(
                        crop, min(cfg.dm_timeout_ms, int(remaining_ms))
                    )
                    if hits:
                        return self._results(
                            hits, frame, origin, "pylibdmtx", started
                        )

            # Step 3: preprocessing variants, only for a stubborn pass.
            if ctx.frames_attempted < cfg.fallback_after_frames:
                return []
            remaining_s = deadline - time.perf_counter()
            if remaining_s <= 0.002:
                return []
            ctx.fallback_runs += 1
            self.counters.fallback_calls += 1
            symbologies = tuple(cfg.symbology_priority)
            futures = [
                self._executor.submit(
                    _variant_task, name, crop, symbologies, cfg.dm_timeout_ms
                )
                for name, _ in preprocess.VARIANTS
            ]
            try:
                pending = set(futures)
                while pending:
                    remaining_s = deadline - time.perf_counter()
                    if remaining_s <= 0:
                        break
                    done, pending = wait(
                        pending, timeout=remaining_s, return_when=FIRST_COMPLETED
                    )
                    if not done:
                        break
                    for fut in done:
                        _, decoder, hits = fut.result()
                        if hits:
                            return self._results(
                                hits, frame, origin, decoder, started
                            )
                return []
            finally:
                for fut in futures:
                    fut.cancel()
        finally:
            ctx.frames_attempted += 1
            if time.perf_counter() > deadline:
                self.counters.budget_overruns += 1
