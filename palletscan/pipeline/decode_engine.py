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

Once a pass is confirmed (``decode.confirmations`` corroborating decodes of
one payload), the cheap inline cascade keeps running — a second pallet can
share the motion segment — but the expensive variant fan-out stays off.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Executor, wait
from dataclasses import dataclass, field
from functools import lru_cache

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


#: Once a pass is confirmed, the Data Matrix probe — which burns its full
#: native timeout whenever no symbol is present — runs only every Nth frame;
#: pyzbar stays per-frame. A second pallet sharing the segment is still
#: caught within ~N frames of becoming decodable.
_CONFIRMED_DM_STRIDE = 5


@dataclass(slots=True)
class _Counters:
    pyzbar_calls: int = 0
    dmtx_calls: int = 0
    fallback_calls: int = 0
    budget_overruns: int = 0


def _decode_sym(
    pyzbar_dec: PyzbarDecoder,
    dmtx_dec: PylibdmtxDecoder,
    sym: Symbology,
    img: np.ndarray,
    dm_timeout_ms: int,
) -> tuple[str, list[RawDecode]]:
    """The one symbology -> decoder dispatch, shared by the inline cascade
    and the preprocessing-variant tasks."""
    if sym is Symbology.QR:
        return "pyzbar", pyzbar_dec.decode(img)
    if sym is Symbology.DATAMATRIX:
        return "pylibdmtx", dmtx_dec.decode(img, dm_timeout_ms)
    return "", []


@lru_cache(maxsize=1)
def _task_decoders() -> tuple[PyzbarDecoder, PylibdmtxDecoder]:
    """Stateless decoder singletons for variant tasks (each worker process
    lazily builds its own pair)."""
    return PyzbarDecoder(), PylibdmtxDecoder()


def _variant_task(
    variant_name: str,
    crop: np.ndarray,
    symbologies: tuple[Symbology, ...],
    dm_timeout_ms: int,
) -> tuple[str, str, list[RawDecode]]:
    """Run one preprocessing variant + decoders. Top-level for picklability
    (process executor support)."""
    processed = preprocess.VARIANTS_BY_NAME[variant_name](crop)
    pyzbar_dec, dmtx_dec = _task_decoders()
    for sym in symbologies:
        name, hits = _decode_sym(pyzbar_dec, dmtx_dec, sym, processed, dm_timeout_ms)
        if hits:
            mapped = [
                RawDecode(
                    payload=h.payload,
                    symbology=h.symbology,
                    roi=preprocess.map_roi_back(variant_name, h.roi, crop.shape),
                )
                for h in hits
            ]
            return variant_name, f"{name}+{variant_name}", mapped
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

    def _count_call(self, sym: Symbology) -> None:
        if sym is Symbology.QR:
            self.counters.pyzbar_calls += 1
        elif sym is Symbology.DATAMATRIX:
            self.counters.dmtx_calls += 1

    def decode_frame(
        self, frame: Frame, roi: Roi, ctx: PassDecodeContext
    ) -> list[DecodeResult]:
        """Run the cascade on one frame's ROI. Returns [] if nothing decoded."""
        cfg = self._cfg
        started = time.perf_counter()
        deadline = started + cfg.frame_budget_ms / 1000.0
        c = roi.clamp(frame.image.shape)
        # One contiguous copy up front: the per-decoder ascontiguousarray
        # calls then become no-ops instead of copying the crop per decoder.
        crop = np.ascontiguousarray(frame.image[c.y : c.y + c.h, c.x : c.x + c.w])
        origin = (c.x, c.y)

        try:
            # Steps 1+2: plain decoders in priority order. These keep
            # running after confirmation — a second pallet can share the
            # motion segment.
            for sym in cfg.symbology_priority:
                dm_ms = cfg.dm_timeout_ms
                if sym is Symbology.DATAMATRIX:
                    if ctx.confirmed and ctx.frames_attempted % _CONFIRMED_DM_STRIDE:
                        continue
                    remaining_ms = (deadline - time.perf_counter()) * 1000.0
                    if remaining_ms <= 1:
                        break
                    dm_ms = min(cfg.dm_timeout_ms, int(remaining_ms))
                self._count_call(sym)
                name, hits = _decode_sym(self._pyzbar, self._dmtx, sym, crop, dm_ms)
                if hits:
                    return self._results(hits, frame, origin, name, started)

            # Step 3: preprocessing variants, only for a stubborn and still
            # unconfirmed pass.
            if ctx.confirmed or ctx.frames_attempted < cfg.fallback_after_frames:
                return []
            remaining_s = deadline - time.perf_counter()
            if remaining_s <= 0.002:
                return []
            ctx.fallback_runs += 1
            self.counters.fallback_calls += 1
            symbologies = tuple(cfg.symbology_priority)
            # Variants get what is left of this frame's budget, not a fresh
            # full dm timeout each.
            dm_ms = min(cfg.dm_timeout_ms, max(1, int(remaining_s * 1000.0)))
            futures = [
                self._executor.submit(_variant_task, name, crop, symbologies, dm_ms)
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
