"""DecodeEngine: cascade order, budget, fallback, early-exit."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from palletscan.config import DecodeConfig
from palletscan.pipeline.decode_engine import DecodeEngine, PassDecodeContext
from palletscan.sources.render import motion_blur, render_datamatrix, render_qr
from palletscan.types import Frame, Roi, Symbology

PAYLOAD = "PLT-000042"


@pytest.fixture()
def executor():
    with ThreadPoolExecutor(max_workers=2) as ex:
        yield ex


def _frame_with(symbol: np.ndarray, at: tuple[int, int] = (60, 80)) -> tuple[Frame, Roi]:
    canvas = np.full((360, 640), 120, np.uint8)
    y, x = at
    canvas[y : y + symbol.shape[0], x : x + symbol.shape[1]] = symbol
    frame = Frame(image=canvas, ts=1.0, frame_index=30, source_id="cam0")
    roi = Roi(x - 20, y - 20, symbol.shape[1] + 40, symbol.shape[0] + 40)
    return frame, roi


def test_clean_qr_uses_fast_path_only(executor) -> None:
    engine = DecodeEngine(DecodeConfig(), executor)
    frame, roi = _frame_with(render_qr(PAYLOAD, 4.0).image)
    results = engine.decode_frame(frame, roi, PassDecodeContext())
    assert [r.payload for r in results] == [PAYLOAD]
    assert results[0].decoder == "pyzbar"
    assert engine.counters.dmtx_calls == 0
    assert engine.counters.fallback_calls == 0
    # ROI mapped back to frame coordinates
    assert results[0].roi.x >= 60 and results[0].roi.y >= 30


def test_datamatrix_decodes_via_cascade(executor) -> None:
    engine = DecodeEngine(DecodeConfig(), executor)
    frame, roi = _frame_with(render_datamatrix(PAYLOAD, 4.0).image)
    results = engine.decode_frame(frame, roi, PassDecodeContext())
    assert [r.payload for r in results] == [PAYLOAD]
    assert results[0].decoder == "pylibdmtx"
    assert results[0].symbology is Symbology.DATAMATRIX


def test_blurred_qr_recovered_by_fallback_after_n_frames(executor) -> None:
    cfg = DecodeConfig(fallback_after_frames=4, frame_budget_ms=250.0)
    engine = DecodeEngine(cfg, executor)
    blurred = motion_blur(render_qr(PAYLOAD, 4.0).image, 4.5)
    frame, roi = _frame_with(blurred)
    ctx = PassDecodeContext()
    outcomes = []
    for _ in range(6):
        batch = engine.decode_frame(frame, roi, ctx)
        outcomes.append(batch)
        if batch:  # the PassTracker confirms on first decode
            ctx.confirmed = True
    flat = [r for batch in outcomes for r in batch]
    assert [r.payload for r in flat] == [PAYLOAD]
    assert "+" in flat[0].decoder  # decoded by a preprocessing variant
    # no fallback during the first fallback_after_frames attempts
    assert all(not outcomes[i] for i in range(4))
    assert ctx.fallback_runs >= 1


def test_confirmed_keeps_inline_cascade_but_skips_fallback(executor) -> None:
    cfg = DecodeConfig(fallback_after_frames=0)
    engine = DecodeEngine(cfg, executor)
    # A decodable symbol must still decode after confirmation — a second
    # pallet can share the motion segment.
    frame, roi = _frame_with(render_qr(PAYLOAD, 4.0).image)
    ctx = PassDecodeContext(confirmed=True)
    hits = engine.decode_frame(frame, roi, ctx)
    assert [r.payload for r in hits] == [PAYLOAD]
    assert engine.counters.pyzbar_calls == 1
    # Undecodable noise: the expensive variant fan-out stays off while
    # the pass is confirmed.
    noise = np.random.default_rng(0).integers(0, 255, (200, 200), np.uint8)
    nframe, nroi = _frame_with(noise)
    assert engine.decode_frame(nframe, nroi, ctx) == []
    assert engine.counters.fallback_calls == 0


def test_qr_only_priority_never_calls_pylibdmtx(executor) -> None:
    cfg = DecodeConfig(symbology_priority=[Symbology.QR], fallback_after_frames=0)
    engine = DecodeEngine(cfg, executor)
    # undecodable noise so the full cascade (incl. fallback) runs
    noise = np.random.default_rng(0).integers(0, 255, (200, 200), np.uint8)
    frame, roi = _frame_with(noise)
    ctx = PassDecodeContext()
    for _ in range(3):
        engine.decode_frame(frame, roi, ctx)
    assert engine.counters.dmtx_calls == 0
    assert engine.counters.fallback_calls >= 1


def test_frame_budget_bounds_wall_time(executor) -> None:
    cfg = DecodeConfig(frame_budget_ms=50.0, dm_timeout_ms=40, fallback_after_frames=0)
    engine = DecodeEngine(cfg, executor)
    noise = np.random.default_rng(1).integers(0, 255, (300, 300), np.uint8)
    frame, roi = _frame_with(noise, at=(0, 0))
    ctx = PassDecodeContext(frames_attempted=10)  # fallback eligible
    start = time.perf_counter()
    result = engine.decode_frame(frame, roi, ctx)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert result == []
    # soft budget: bounded by budget + one in-flight C call, not unbounded
    assert elapsed_ms < 400


def test_dm_priority_order_runs_dmtx_first(executor) -> None:
    cfg = DecodeConfig(
        symbology_priority=[Symbology.DATAMATRIX, Symbology.QR],
        frame_budget_ms=200.0,
    )
    engine = DecodeEngine(cfg, executor)
    frame, roi = _frame_with(render_datamatrix(PAYLOAD, 4.0).image)
    results = engine.decode_frame(frame, roi, PassDecodeContext())
    assert results and results[0].decoder == "pylibdmtx"
    assert engine.counters.pyzbar_calls == 0


def test_payload_gate_default_drops_garbage_keeps_text(executor) -> None:
    # REVIEW DEC-01: with no pattern the gate is permissive — it drops only
    # empty / control-byte garbage, never normal printable text.
    engine = DecodeEngine(DecodeConfig(), executor)
    assert engine._accept("PLT-000001")
    assert engine._accept("Test DM 4.5 inches")
    assert engine._accept("a\tb\nc")  # tab/newline-bearing text is fine
    assert not engine._accept("")  # empty
    assert not engine._accept("F\x01m")  # C0 control byte = decoder false-positive


def test_payload_pattern_rejects_phantom_and_counts(executor) -> None:
    # A configured pattern drops a spurious-but-valid decode (the "F'm" phantom)
    # BEFORE it can become a confirmed pass, and counts it as spurious_rejected.
    from palletscan.pipeline.decoders import RawDecode

    cfg = DecodeConfig(payload_pattern=r"^PLT-\d{6}$")
    engine = DecodeEngine(cfg, executor)
    frame, _roi = _frame_with(np.zeros((8, 8), np.uint8))
    good = RawDecode(payload="PLT-000007", symbology=Symbology.QR, roi=Roi(0, 0, 2, 2))
    phantom = RawDecode(payload="F'm", symbology=Symbology.QR, roi=Roi(0, 0, 2, 2))
    out = engine._results([good, phantom], frame, (0, 0), "test", time.perf_counter())
    assert [r.payload for r in out] == ["PLT-000007"]
    assert engine.counters.spurious_rejected == 1
