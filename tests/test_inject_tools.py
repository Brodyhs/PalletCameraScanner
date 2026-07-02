"""Stale consumers of the truth TIME-BASE change (re-review of
REVIEW_bringup_4d95b67): CameraInjectionSource records truth
``first_frame``/``last_frame`` as nominal-fps ticks of the live ts clock,
NOT camera frame indices — tools/inject_run.py and tools/inject_smoke.py
kept joining those ticks against frame indices, which diverge from ts under
outages / connect time / real-vs-nominal fps error."""

from __future__ import annotations

from palletscan.types import GroundTruthRecord, MissEvent, PassEvent, Symbology
from tools.inject_run import _account
from tools.inject_smoke import pick_mid_pass_frame

FPS = 30.0
#: ts-vs-frame_index offset (seconds): the live clock ran through a watchdog
#: outage / connect time / shared-camera reuse before these frames arrived.
TS0 = 120.0


def _truth(payload: str, ts_first: float, ts_last: float) -> GroundTruthRecord:
    return GroundTruthRecord(
        pass_id=0,
        payload=payload,
        symbology=Symbology.QR,
        first_frame=round(ts_first * FPS),  # nominal-fps ticks of the ts clock
        last_frame=round(ts_last * FPS),
        params={},
    )


def _miss(start_ts: float, end_ts: float, first_frame: int, last_frame: int) -> MissEvent:
    return MissEvent(
        candidate_id="cand-1",
        source_id="cam0",
        start_ts=start_ts,
        end_ts=end_ts,
        first_frame=first_frame,  # REAL camera frame indices
        last_frame=last_frame,
        evidence_dir="",
        evidence_frame_count=0,
        event_id="ev-1",
        wall_time_iso="",
    )


def _pass(payload: str, ts: float) -> PassEvent:
    return PassEvent(
        payload=payload,
        symbology=Symbology.QR,
        first_seen_ts=ts,
        last_seen_ts=ts + 0.5,
        decode_count=1,
        cameras={"cam0": 1},
        best_frame=("cam0", 0),
        candidate_ids=["cand-2"],
        event_id="ev-2",
        wall_time_iso="",
        first_decode_ts=ts + 0.1,
    )


def test_inject_run_accounts_misses_by_ts_not_frame_index_overlap() -> None:
    """inject_run's sweep report: a genuinely missed injected pass whose
    miss event carries real camera frame indices (here 25..55) while truth
    carries ts ticks (here ~3630..3660, offset by the outage) must be
    counted 'flagged-miss', not 'not-flagged/unaccounted' — the pre-fix
    frame-index overlap found no intersection between the two units."""
    missed_rec = _truth("INJ-1", TS0 + 1.0, TS0 + 2.0)  # ticks 3630..3660
    decoded_rec = _truth("INJ-2", TS0 + 5.0, TS0 + 6.0)
    events = [
        _miss(TS0 + 0.9, TS0 + 2.1, first_frame=25, last_frame=55),
        _pass("INJ-2", TS0 + 5.2),
    ]
    decoded, missed, unacc = _account([missed_rec, decoded_rec], events, FPS)
    assert decoded == 1
    assert missed == 1
    assert unacc == 0, "the flagged miss was reported not-flagged"


def test_inject_smoke_picks_the_mid_pass_frame_by_ts() -> None:
    """inject_smoke's Phase-A evidence PNG: with ~2 s of connect time before
    frame 0, truth ticks lead frame indices by connect_s*fps, so the pre-fix
    ``frame_index ~ (first_frame+last_frame)//2`` match picked a frame after
    the code had fully exited (typically the last buffered one) — a
    code-free PNG stamped PHASE A OK. The pick must match on ts."""
    connect_s = 2.0
    frames = [(i, None, connect_s + i / FPS) for i in range(90)]
    # Pass visible on frame_index 30..60 -> ts 3.0..4.0 -> ticks 90..120.
    rec = _truth("INJ-1", 3.0, 4.0)
    pick = pick_mid_pass_frame(frames, rec, FPS)
    assert pick[0] == 45, "picked a frame outside the pass"
    # The pre-fix index match landed on the LAST buffered frame (index 89),
    # nearest to tick 105 — well past the pass's real 30..60 span.
    assert pick[0] != min(
        frames, key=lambda f: abs(f[0] - (rec.first_frame + rec.last_frame) // 2)
    )[0]
