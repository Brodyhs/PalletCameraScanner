"""End-to-end TIER 2 wiring: the real MotionGate (multi mode) output feeding a
real PassTracker, for the core account-for-everything case — two concurrent
objects, ONE decodes and ONE does not.

The single-component tests cover the gate (test_motion_tracking.py) and the
tracker (test_pass_tracker.py) in isolation, but nothing wired the genuine
gate->tracker seam: a real MotionGate segmenting two blobs into two concurrent
segments, each ROI decoded independently and routed back by candidate_id. This
mirrors ``app.py:_process_frame`` (opens first, then per-OPEN-track decode
routed by ``track.track_id``, then closes, then post-roll finalize) so a
decoded pallet provably can no longer swallow a co-located undecoded pallet's
MissEvent.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from palletscan.app import PipelineRunner
from palletscan.config import (
    AppConfig,
    BufferConfig,
    DecodeConfig,
    DedupConfig,
    EvidenceConfig,
    MotionConfig,
    apply_overrides,
)
from palletscan.events.evidence import EvidenceWriter
from palletscan.pipeline.motion_gate import MotionGate
from palletscan.pipeline.pass_tracker import PassTracker
from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.types import (
    DecodeResult,
    Frame,
    MissEvent,
    MotionResult,
    MotionTrack,
    PassEvent,
    Roi,
    SegmentEvent,
    SegmentKind,
    Symbology,
)

H, W = 360, 640
BG = 90
FPS = 30.0

# The decoded object rides the TOP band; the undecoded one rides the BOTTOM
# band. A decode is injected only for the track whose ROI centre is up top.
TOP_Y = 110
BOTTOM_Y = 250
SPLIT_Y = (TOP_Y + BOTTOM_Y) // 2


def _frame(image: np.ndarray, idx: int) -> Frame:
    return Frame(image=image, ts=idx / FPS, frame_index=idx, source_id="cam0")


def _blank() -> np.ndarray:
    return np.full((H, W), BG, np.uint8)


def _val(i: int) -> int:
    return 200 + (i % 2) * 40


def _square(img: np.ndarray, cx: int, cy: int, val: int, size: int = 80) -> None:
    half = size // 2
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(W, cx + half), min(H, cy + half)
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = val


def _two_object_stream(n_move: int, n_quiet: int) -> list[np.ndarray]:
    """A rides the top band L->R; B rides the bottom band R->L. Well separated
    in y so the real gate yields two stable, independent blobs throughout.

    A trailing run of blank (no-motion) frames lets BOTH tracks close through
    the gate's ordinary quiet-aging path, then carries the source clock far
    enough past the undecoded track's post-roll deadline that its MissEvent
    finalizes through the normal deadline path — no end-of-stream flush needed.
    That is what makes this test sensitive to a dropped CLOSE: a flush backstop
    would re-emit a swallowed miss and mask the bug."""
    frames = []
    for i in range(n_move):
        img = _blank()
        _square(img, 60 + i * 8, TOP_Y, _val(i))  # A (top): will be decoded
        _square(img, W - 60 - i * 8, BOTTOM_Y, _val(i))  # B (bottom): no decode
        frames.append(img)
    frames.extend(_blank() for _ in range(n_quiet))  # motion ceases -> tracks close
    return frames


def _decode_for(roi: Roi, frame: Frame, payload: str) -> list[DecodeResult]:
    return [
        DecodeResult(
            payload=payload,
            symbology=Symbology.QR,
            roi=roi,
            frame_index=frame.frame_index,
            ts=frame.ts,
            source_id="cam0",
            decoder="injected",
            latency_ms=1.0,
        )
    ]


def test_one_decodes_one_misses_end_to_end(tmp_path: Path) -> None:
    """Real MotionGate(multi) -> real PassTracker. Two concurrent objects; only
    the top one ever decodes. EXACTLY one PassEvent (the decoded payload) and
    one MissEvent (the undecoded track), with distinct candidate_ids and
    distinct evidence. If the undecoded track's CLOSE were dropped (its miss
    swallowed by the decoded segment), there would be no MissEvent and this
    fails."""
    gate = MotionGate(
        MotionConfig(tracking="multi", open_frames=3, quiet_frames=5),
        "cam0",
        run_token="t0",
    )
    buffer = RollingFrameBuffer(horizon_s=5.0)
    events: list = []
    tracker = PassTracker(
        dedup_cfg=DedupConfig(window_s=12.0),
        buffer_cfg=BufferConfig(pre_s=2.0, post_s=2.0),
        evidence=EvidenceWriter(EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)),
        buffer=buffer,
        emit=events.append,
        source_id="cam0",
    )

    def process(frame: Frame) -> None:
        # Mirror app.py:_process_frame's gate->tracker seam.
        tracker.on_frame(frame)
        buffer.append(frame)
        result, seg_events = gate.update(frame)
        for ev in seg_events:  # opens first, so a same-frame decode sees its ctx
            if ev.kind is SegmentKind.OPEN:
                tracker.on_segment_open(ev)
        for track in result.tracks:
            ctx = tracker.ctx_for(track.track_id)
            if ctx is None:
                continue
            # Inject a decode ONLY for the top-band (A) track; the bottom-band
            # (B) track is left to miss — this is the one-decodes-one-misses
            # split the real decoder would produce if only A carried a readable
            # code.
            if track.roi.y + track.roi.h / 2 < SPLIT_Y:
                tracker.on_decode(track.track_id, _decode_for(track.roi, frame, "PLT-TOP"))
        for ev in seg_events:
            if ev.kind is SegmentKind.CLOSE:
                tracker.on_segment_close(ev)

    # Long enough quiet tail that the gate quiet-closes both tracks AND the
    # source clock advances well past the undecoded track's post-roll deadline,
    # so its MissEvent finalizes through the normal deadline path (no flush).
    for i, img in enumerate(_two_object_stream(22, 90)):
        process(_frame(img, i))

    # Intentionally NO tracker.flush(): the run completes through the natural
    # lifecycle. (A flush would backstop a dropped CLOSE and hide the bug this
    # test exists to catch.)

    passes = [e for e in events if isinstance(e, PassEvent)]
    misses = [e for e in events if isinstance(e, MissEvent)]

    assert len(passes) == 1, [getattr(e, "payload", e) for e in events]
    assert len(misses) == 1, [getattr(e, "candidate_id", e) for e in events]

    p = passes[0]
    m = misses[0]
    assert p.payload == "PLT-TOP"
    # Distinct candidates: the decoded pass and the undecoded miss are NOT the
    # same segment, and the miss is not folded into the pass.
    assert m.candidate_id not in p.candidate_ids
    assert len(p.candidate_ids) == 1
    # Distinct, real evidence for the miss (its own reservoir).
    assert m.evidence_dir and Path(m.evidence_dir).is_dir()
    assert m.evidence_error is None
    assert tracker.passes_emitted == 1
    assert tracker.misses_emitted == 1


# -- app.py _process_frame seam: per-track decode budget + async idle scan -----
#
# These drive PipelineRunner._process_frame directly (the test_pipeline_smoke
# manual-drive pattern) with a scripted gate / recording engine, so the budget
# and idle-scan scheduling can be observed deterministically without threads
# racing a live source.


class _ManualSource:
    """Minimal FrameSource stand-in for driving _process_frame directly."""

    def __init__(self) -> None:
        self.live = False
        self.source_id = "cam0"
        self.nominal_fps = FPS

    def frames(self):  # pragma: no cover - not used when driven manually
        return iter(())

    def close(self) -> None:
        pass


def _manual_runner(tmp_path: Path, **cfg_updates) -> PipelineRunner:
    cfg = apply_overrides(AppConfig(), data_dir=tmp_path).model_copy(
        update=cfg_updates
    )
    return PipelineRunner(cfg, _ManualSource(), sinks=[])


class _ScriptedGate:
    """MotionGate stand-in: the same open multi-mode tracks every frame."""

    def __init__(self, rois: dict[str, Roi]) -> None:
        self._rois = rois
        self._opened = False

    def update(self, frame: Frame):
        events = []
        if not self._opened:
            self._opened = True
            events = [
                SegmentEvent(
                    kind=SegmentKind.OPEN,
                    candidate_id=tid,
                    frame_index=frame.frame_index,
                    ts=frame.ts,
                )
                for tid in self._rois
            ]
        tracks = tuple(
            MotionTrack(
                track_id=tid,
                roi=roi,
                centroid=(0.0, 0.0),
                area_px=100,
                age=2,
                missed=0,
            )
            for tid, roi in self._rois.items()
        )
        first = next(iter(self._rois))
        return (
            MotionResult(
                active=True,
                candidate_id=first,
                roi=self._rois[first],
                motion_frac=0.5,
                tracks=tracks,
            ),
            events,
        )

    def flush(self):
        return []

    def break_segment(self):
        return []


class _RecordingEngine:
    """DecodeEngine stand-in: records (frame_index, roi.x), can burn time."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self.sleep_s = sleep_s
        self.calls: list[tuple[int, int]] = []

    def decode_frame(self, frame: Frame, roi: Roi, ctx) -> list[DecodeResult]:
        self.calls.append((frame.frame_index, roi.x))
        if self.sleep_s:
            time.sleep(self.sleep_s)
        ctx.frames_attempted += 1  # what the real engine's finally block does
        return []


def test_track_decodes_share_one_frame_budget_and_rotate(tmp_path: Path) -> None:
    """REVIEW_bringup_4d95b67 finding 15: each open track used to get a FULL
    fresh frame_budget_ms decode_frame call (budget x up to track_max_objects
    on the single pipeline thread). The per-track calls must share ONE
    per-frame budget — no further track decodes once it is exhausted — and the
    starting track must rotate by frame_index so a budget-burning ROI cannot
    permanently starve the other tracks."""
    runner = _manual_runner(tmp_path, decode=DecodeConfig(frame_budget_ms=20.0))
    rois = {f"trk{i}": Roi(i * 10, 0, 10, 10) for i in range(3)}
    runner._gate = _ScriptedGate(rois)  # type: ignore[assignment]
    engine = _RecordingEngine(sleep_s=0.05)  # every call overruns the budget
    runner._engine = engine  # type: ignore[assignment]
    img = np.full((H, W), BG, np.uint8)
    for i in range(3):
        runner._process_frame(_frame(img, i))
    per_frame: dict[int, list[int]] = {}
    for fi, x in engine.calls:
        per_frame.setdefault(fi, []).append(x)
    # A 50 ms call exhausts the 20 ms budget: exactly ONE decode per frame
    # (pre-fix: all three tracks each got a fresh budget, every frame).
    assert {fi: len(xs) for fi, xs in per_frame.items()} == {0: 1, 1: 1, 2: 1}
    # The starting track rotates with frame_index: every track gets its turn.
    assert [per_frame[i][0] for i in range(3)] == [0, 10, 20]
    runner._executor.shutdown(wait=True)


def test_all_open_tracks_decode_when_budget_allows(tmp_path: Path) -> None:
    """Budget sharing must not starve anyone when there is time: with a fast
    engine, every open track still decodes every frame."""
    runner = _manual_runner(tmp_path, decode=DecodeConfig(frame_budget_ms=5000.0))
    rois = {f"trk{i}": Roi(i * 10, 0, 10, 10) for i in range(3)}
    runner._gate = _ScriptedGate(rois)  # type: ignore[assignment]
    engine = _RecordingEngine()
    runner._engine = engine  # type: ignore[assignment]
    img = np.full((H, W), BG, np.uint8)
    for i in range(2):
        runner._process_frame(_frame(img, i))
    per_frame: dict[int, set[int]] = {}
    for fi, x in engine.calls:
        per_frame.setdefault(fi, set()).add(x)
    assert per_frame == {0: {0, 10, 20}, 1: {0, 10, 20}}
    runner._executor.shutdown(wait=True)


class _IdleEngine:
    """Records the ctx + thread of each idle decode; queues canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, int, threading.Thread]] = []
        self.results: list[list[DecodeResult]] = []

    def decode_frame(self, frame: Frame, roi: Roi, ctx) -> list[DecodeResult]:
        self.calls.append((ctx, ctx.frames_attempted, threading.current_thread()))
        ctx.frames_attempted += 1  # mirrors the real engine's finally block
        return self.results.pop(0) if self.results else []


def test_idle_scan_persists_context_and_runs_off_thread(tmp_path: Path) -> None:
    """REVIEW_bringup_4d95b67 idle-scan finding: (a) a FRESH PassDecodeContext
    per scan pinned frames_attempted at 0, so the step-3 variant fan-out could
    never engage for a stubborn static code — one context must persist across
    failed scans and reset only when an idle decode succeeds; (b) the scan ran
    synchronously on the pipeline consumer thread, where the legacy pyzbar
    full-frame step has no timeout — it must run on the decode executor."""
    runner = _manual_runner(tmp_path, motion=MotionConfig(idle_scan_s=0.5))
    engine = _IdleEngine()
    # The idle scan runs on its own engine (never the pipeline thread's —
    # sharing one raced its state across threads, re-review).
    runner._idle_engine = engine  # type: ignore[assignment]
    img = np.full((H, W), BG, np.uint8)

    def drive(i: int, ts: float) -> None:
        runner._process_frame(Frame(image=img, ts=ts, frame_index=i, source_id="cam0"))
        fut = runner._idle_future
        if fut is not None:
            fut.result(timeout=5.0)  # let any submitted scan finish

    drive(0, 1.0)
    drive(1, 2.0)
    drive(2, 3.0)
    assert len(engine.calls) == 3
    ctxs = [c for c, _, _ in engine.calls]
    # (a) ONE persistent context accrues attempts across failed scans.
    assert ctxs[0] is ctxs[1] is ctxs[2]
    assert [n for _, n, _ in engine.calls] == [0, 1, 2]
    # (b) every scan ran off the calling (pipeline) thread.
    assert all(t is not threading.current_thread() for _, _, t in engine.calls)

    # A successful idle read is harvested on a later frame, counts in
    # idle_reads, and resets the persistent context.
    frame3 = Frame(image=img, ts=4.0, frame_index=3, source_id="cam0")
    engine.results.append(_decode_for(Roi(0, 0, W, H), frame3, "STATIC-1"))
    drive(3, 4.0)
    runner._process_frame(
        Frame(image=img, ts=4.2, frame_index=4, source_id="cam0")
    )  # harvest only: 4.2 - 4.0 < idle_scan_s, no new submit
    assert runner._idle_reads == 1
    assert runner._idle_ctx is not ctxs[0]
    assert runner._idle_ctx.frames_attempted == 0
    runner._executor.shutdown(wait=True)


def test_only_one_idle_scan_in_flight(tmp_path: Path) -> None:
    """A slow idle scan must neither stall the pipeline thread nor stack up:
    while one scan is in flight no second scan launches, and the next scan
    goes out only after the first completes."""
    runner = _manual_runner(tmp_path, motion=MotionConfig(idle_scan_s=0.5))
    release = threading.Event()
    calls: list[int] = []

    class _BlockingEngine:
        def decode_frame(self, frame: Frame, roi: Roi, ctx) -> list[DecodeResult]:
            calls.append(frame.frame_index)
            release.wait(2.0)
            return []

    runner._idle_engine = _BlockingEngine()  # type: ignore[assignment]
    img = np.full((H, W), BG, np.uint8)
    started = time.perf_counter()
    runner._process_frame(Frame(image=img, ts=1.0, frame_index=0, source_id="cam0"))
    # The pipeline thread is not blocked by the in-flight scan...
    assert time.perf_counter() - started < 1.0
    runner._process_frame(Frame(image=img, ts=2.0, frame_index=1, source_id="cam0"))
    runner._process_frame(Frame(image=img, ts=3.0, frame_index=2, source_id="cam0"))
    # ...and no second scan launched while the first was in flight.
    assert calls == [0]
    release.set()
    fut = runner._idle_future
    assert fut is not None
    fut.result(timeout=5.0)
    runner._process_frame(Frame(image=img, ts=4.0, frame_index=3, source_id="cam0"))
    fut = runner._idle_future
    assert fut is not None  # slot freed -> the next scan went out
    fut.result(timeout=5.0)
    assert calls == [0, 3]
    runner._executor.shutdown(wait=True)


def test_idle_scan_fanout_executes_with_a_single_decode_worker(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review of REVIEW_bringup_4d95b67 (app.py idle scan): the async
    idle scan was submitted to the SAME bounded decode executor its own
    step-3 variant fan-out uses, so with decode.workers: 1 the scan occupied
    the only worker, its variant futures could never start, and it burned
    the entire frame budget before returning [] — the stubborn static code
    the fix existed for was never variant-decoded. The scan must run on its
    own dedicated thread (and its own engine, so no engine state is shared
    across threads) while the variants fan out on the decode executor."""
    import palletscan.pipeline.decode_engine as de

    ran: list[str] = []

    def instant_variant(name, crop, symbologies, dm_ms):
        ran.append(threading.current_thread().name)
        return name, "", []

    monkeypatch.setattr(de, "_variant_task", instant_variant)
    runner = _manual_runner(
        tmp_path,
        motion=MotionConfig(idle_scan_s=0.5),
        decode=DecodeConfig(
            workers=1, fallback_after_frames=0, frame_budget_ms=10_000.0
        ),
    )
    assert runner._idle_engine is not runner._engine  # no shared engine state
    img = np.full((H, W), BG, np.uint8)
    runner._process_frame(Frame(image=img, ts=1.0, frame_index=0, source_id="cam0"))
    fut = runner._idle_future
    assert fut is not None
    # Pre-fix this timed out: the scan held the pool's only worker while its
    # own fan-out futures sat queued behind it until the 10 s budget expired.
    fut.result(timeout=5.0)
    assert ran, "the step-3 variant fan-out never executed"
    # The variants ran on the decode pool; the scan itself did not occupy it.
    assert all(t.startswith("decode") for t in ran)
    assert runner._idle_engine.counters.fallback_calls == 1
    runner._idle_executor.shutdown(wait=True)
    runner._executor.shutdown(wait=True)
