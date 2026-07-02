"""DecodeEngine: cascade order, budget, fallback, early-exit."""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from palletscan.config import DecodeConfig, DecodeEngineKind
from palletscan.pipeline.decode_engine import DecodeEngine, PassDecodeContext
from palletscan.pipeline.decoders import RawDecode
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
    assert engine._accept("PLT-000001", Symbology.QR)
    assert engine._accept("Test DM 4.5 inches", Symbology.DATAMATRIX)
    assert engine._accept("a\tb\nc", Symbology.QR)  # tab/newline text is fine
    assert not engine._accept("", Symbology.QR)  # empty
    assert not engine._accept("F\x01m", Symbology.QR)  # C0 = false-positive


def test_payload_gate_accepts_gs1_and_iso15434_separators(executor) -> None:
    # Standard warehouse label content embeds C0 separator bytes: GS1 QR/DM
    # use GS (0x1D) as the FNC1/AI separator; ISO 15434 envelopes use
    # RS/GS/EOT (and FS). The default gate must pass these, while still
    # rejecting empty payloads and other C0 garbage (the F\x00m phantom).
    engine = DecodeEngine(DecodeConfig(), executor)
    assert engine._accept(
        "0100345312000023\x1d10ABC123\x1d21000042", Symbology.DATAMATRIX
    )  # GS1
    assert engine._accept(
        "[)>\x1e06\x1d1JUN123456\x1d20L\x1e\x04", Symbology.DATAMATRIX
    )  # ISO 15434
    assert not engine._accept("F\x00m", Symbology.QR)  # NUL false-positive
    assert not engine._accept("", Symbology.QR)  # empty


def test_payload_gate_rejects_short_datamatrix_misdecodes(executor) -> None:
    # pylibdmtx's characteristic false-positive on a noisy crop is a SHORT
    # printable payload ("F'm" — all-printable, so the control-byte check
    # passes it). The default gate requires dm_min_payload_len (4) for
    # DATAMATRIX results only; QR is unaffected, and the knob is tunable.
    engine = DecodeEngine(DecodeConfig(), executor)
    assert not engine._accept("F'm", Symbology.DATAMATRIX)  # the phantom
    assert engine._accept("F'm", Symbology.QR)  # QR path unaffected
    assert engine._accept("PLT-000001", Symbology.DATAMATRIX)
    permissive = DecodeEngine(DecodeConfig(dm_min_payload_len=1), executor)
    assert permissive._accept("F'm", Symbology.DATAMATRIX)  # knob relaxes it
    strict_re = DecodeEngine(
        DecodeConfig(payload_pattern="^F'm$", dm_min_payload_len=4), executor
    )
    assert strict_re._accept("F'm", Symbology.DATAMATRIX)  # pattern overrides


def test_gate_rejected_hits_do_not_stop_legacy_cascade(executor) -> None:
    # A decoder step whose hits are ALL gate-rejected must not short-circuit
    # decode_frame: the remaining symbology decoders still get their turn.
    class _PhantomPyzbar:
        def decode(self, gray: np.ndarray) -> list[RawDecode]:
            return [
                RawDecode(payload="F\x00m", symbology=Symbology.QR, roi=Roi(0, 0, 2, 2))
            ]

    engine = DecodeEngine(DecodeConfig(), executor)
    engine._pyzbar = _PhantomPyzbar()  # type: ignore[assignment]
    frame, roi = _frame_with(render_datamatrix(PAYLOAD, 4.0).image)
    results = engine.decode_frame(frame, roi, PassDecodeContext())
    assert [r.payload for r in results] == [PAYLOAD]
    assert results[0].decoder == "pylibdmtx"
    assert engine.counters.spurious_rejected == 1


def test_rejection_warns_once_per_payload_then_debug(executor, caplog) -> None:
    # A recurring rejected payload logs WARNING only on first sight (DEBUG
    # after) so decoder false-positives can't spam per-frame logging I/O;
    # the spurious_rejected counter stays exact, and each DISTINCT payload
    # still gets its own first-sight WARNING.
    engine = DecodeEngine(DecodeConfig(), executor)
    frame, _roi = _frame_with(np.zeros((8, 8), np.uint8))
    bad = RawDecode(payload="F\x00m", symbology=Symbology.QR, roi=Roi(0, 0, 2, 2))
    other = RawDecode(payload="F\x01m", symbology=Symbology.QR, roi=Roi(0, 0, 2, 2))
    with caplog.at_level(logging.DEBUG, logger="palletscan.pipeline.decode_engine"):
        for _ in range(3):
            engine._results([bad], frame, (0, 0), "test", time.perf_counter())
        engine._results([other], frame, (0, 0), "test", time.perf_counter())
    records = [r for r in caplog.records if "rejected non-conforming" in r.getMessage()]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    debugs = [r for r in records if r.levelno == logging.DEBUG]
    assert len(warnings) == 2  # one per distinct payload
    assert len(debugs) == 2  # repeats of the first payload
    assert engine.counters.spurious_rejected == 4


def test_zxing_engine_import_failure_is_loud_and_actionable(
    executor, monkeypatch
) -> None:
    # decode.engine: zxing with zxing-cpp missing must fail AT CONSTRUCTION
    # with a message naming the config key and the install fix — never a
    # silent fallback or a bare ImportError later.
    monkeypatch.setitem(sys.modules, "zxingcpp", None)  # import -> ImportError
    with pytest.raises(RuntimeError, match=r"decode\.engine") as exc_info:
        DecodeEngine(DecodeConfig(engine=DecodeEngineKind.ZXING), executor)
    assert '.[zxing]' in str(exc_info.value)


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
