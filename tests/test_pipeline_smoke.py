"""Full threaded PipelineRunner on easy synthetic passes."""

from __future__ import annotations

import json
import sqlite3
import threading

from palletscan.app import PipelineRunner
from palletscan.config import AppConfig


def test_easy_passes_all_decode_no_misses(fast_synth_config: AppConfig) -> None:
    cfg = fast_synth_config
    before = set(threading.enumerate())
    runner = PipelineRunner.from_config(cfg)
    summary = runner.run()

    # all 3 easy passes decode, nothing missed, nothing unaccounted
    assert summary.reconciliation is not None
    assert summary.reconciliation.truth_passes == 3
    assert summary.reconciliation.decoded == 3
    assert summary.misses == 0
    assert summary.unaccounted == 0
    assert summary.frames > 0

    # outputs landed
    jsonl = cfg.sinks.jsonl.path
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len([l for l in lines if l["kind"] == "pass"]) == 3
    conn = sqlite3.connect(cfg.sinks.sqlite.path)
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='pass'"
    ).fetchone()
    conn.close()
    assert count == 3

    # no leaked threads, queues drained
    leaked = [
        t
        for t in set(threading.enumerate()) - before
        if t.is_alive() and t.name in ("source", "pipeline", "eventbus")
    ]
    assert leaked == []
    assert runner._frame_q.qsize() == 0
    assert runner._bus.queue.qsize() == 0


def test_stop_requests_graceful_early_exit(fast_synth_config: AppConfig) -> None:
    cfg = fast_synth_config.model_copy(
        update={
            "synthetic": fast_synth_config.synthetic.model_copy(
                update={"num_passes": 50, "realtime": True}
            )
        }
    )
    runner = PipelineRunner.from_config(cfg)
    threading.Timer(0.5, runner.stop).start()
    summary = runner.run()  # must return promptly without raising
    assert summary.frames < 1000


# -- REVIEW_SYSTEM_0c30c77 regressions (frame-path findings 2, 4, 11) ----------


class _ManualSource:
    """Minimal FrameSource stand-in for driving _process_frame directly."""

    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.source_id = "cam0"
        self.nominal_fps = 30.0

    def frames(self):  # pragma: no cover - not used when driven manually
        return iter(())

    def close(self) -> None:
        pass


def _runner_for_manual_drive(tmp_path) -> PipelineRunner:
    from palletscan.config import apply_overrides

    cfg = apply_overrides(AppConfig(), data_dir=tmp_path)
    return PipelineRunner(cfg, _ManualSource(), sinks=[])


def test_discontinuity_breaks_segment_p1_miss_p2_pass(tmp_path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 2 (critical) — the review's executed
    repro, inverted: camA opens a segment for pallet P1 (label never
    decodes), the camera stalls mid-pass, the watchdog recovers ~12 s
    later while pallet P2 (decodable) is mid-zone. Old behavior: ONE
    segment spans the gap, P2's decode makes it a pass, P1's MissEvent and
    evidence evaporate (passes_emitted=1, misses_emitted=0). Fixed: the
    discontinuity frame breaks the segment — P1 is a miss with its pre-gap
    candidate id, P2 a pass with a different id."""
    import cv2
    import numpy as np

    from palletscan.selftest import SELFTEST_ASSETS
    from palletscan.types import Frame, MissEvent, PassEvent, Symbology

    payload, asset_path = SELFTEST_ASSETS[Symbology.QR]
    qr = cv2.imread(str(asset_path), cv2.IMREAD_GRAYSCALE)
    assert qr is not None

    runner = _runner_for_manual_drive(tmp_path)
    runner._bus.start()  # manual drive: no run(), so start the bus here
    fps = 30.0
    h, w = 480, 960

    def frame(i: int, ts_offset: float = 0.0, image=None, disc=False) -> Frame:
        img = (
            image
            if image is not None
            else np.full((h, w), 128, np.uint8)
        )
        return Frame(
            image=img,
            ts=i / fps + ts_offset,
            frame_index=i,
            source_id="cam0",
            discontinuity=disc,
        )

    def p1_image(i: int) -> np.ndarray:
        img = np.full((h, w), 128, np.uint8)
        x = 40 + i * 18
        img[140:340, x : x + 180] = 230  # bright pallet, no symbol
        return img

    def p2_image(k: int) -> np.ndarray:
        img = np.full((h, w), 128, np.uint8)
        x = 40 + k * 18
        ah, aw = qr.shape
        img[100 : 100 + ah, x : x + aw] = qr
        return img

    # idle, then P1 motion that never goes quiet before the stall
    for i in range(0, 6):
        runner._process_frame(frame(i))
    for i in range(6, 26):
        runner._process_frame(frame(i, image=p1_image(i - 6)))
    assert runner._tracker.open_ctx is not None, "P1 segment must be open"

    # reconnect: ts jumps 12 s; P2 is mid-zone on the very first frame
    gap = 12.0
    base = 26
    for k in range(0, 30):
        runner._process_frame(
            frame(base + k, ts_offset=gap, image=p2_image(k), disc=(k == 0))
        )
    # P2 leaves; quiet frames close its segment, tail passes the deadline
    for k in range(30, 30 + 12 + int(2.0 * fps) + 2):
        runner._process_frame(frame(base + k, ts_offset=gap))
    # end-of-stream flush, exactly as _pipeline_loop's finally does
    tail = runner._gate.flush()
    if tail is not None:
        runner._tracker.on_segment_close(tail)
    runner._tracker.flush()
    runner._executor.shutdown(wait=True)
    runner._bus.shutdown()

    events = runner.collected_events
    misses = [e for e in events if isinstance(e, MissEvent)]
    passes = [e for e in events if isinstance(e, PassEvent)]
    assert len(misses) == 1, f"P1 must be accounted as a miss: {events}"
    assert len(passes) == 1 and passes[0].payload == payload
    assert misses[0].candidate_id != passes[0].candidate_ids[0]
    # P1's segment closed at its pre-gap last-active frame, so its miss
    # cannot span the outage and P2's timing is not outage-inflated.
    assert misses[0].end_ts <= 26 / fps + 0.01
    assert passes[0].first_seen_ts >= gap


def test_pipeline_death_with_live_source_does_not_hang_run(tmp_path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 4 (reproduced there: _pipeline_dead
    set, error recorded, run() still hung after 6 s with the fake camera
    yielding ~330 fps into the void): a pipeline-thread crash on a live
    source must abort the producer and raise out of run()."""
    import time as _time

    import numpy as np
    import pytest

    from palletscan.config import apply_overrides
    from palletscan.types import Frame

    class _LiveForeverSource:
        live = True
        source_id = "cam0"
        nominal_fps = 30.0

        def __init__(self) -> None:
            self._closed = False
            self._img = np.full((120, 160), 128, np.uint8)

        def frames(self):
            i = 0
            while not self._closed:
                yield Frame(
                    image=self._img, ts=i / 30.0, frame_index=i, source_id="cam0"
                )
                i += 1
                _time.sleep(0.001)

        def close(self) -> None:
            self._closed = True

    cfg = apply_overrides(AppConfig(), data_dir=tmp_path)
    runner = PipelineRunner(cfg, _LiveForeverSource(), sinks=[])
    calls = {"n": 0}
    real_record = runner.metrics.record_frame

    def dying_record(ts: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 5:
            raise MemoryError("simulated pipeline-thread death")
        real_record(ts)

    runner.metrics.record_frame = dying_record  # outside the per-frame try

    done = threading.Event()
    outcome: list = []

    def run_it() -> None:
        try:
            runner.run()
            outcome.append("returned")
        except RuntimeError as exc:
            outcome.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=run_it, daemon=True)
    t.start()
    assert done.wait(timeout=10.0), "run() hung with the pipeline dead (finding 4)"
    assert outcome and isinstance(outcome[0], RuntimeError)


def test_undrained_bus_fails_the_run_loudly(tmp_path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 11: one hung sink write used to let
    run() print a clean summary and exit 0 while the queue tail died with
    the daemon bus thread. An undrained bus must fail the run."""
    import pytest

    from palletscan.events.sinks import Sink

    release = threading.Event()

    class _WedgedSink(Sink):
        def handle(self, event) -> None:
            release.wait(timeout=30.0)

    from palletscan.config import apply_overrides

    cfg = apply_overrides(AppConfig(), data_dir=tmp_path)
    cfg = cfg.model_copy(
        update={"synthetic": cfg.synthetic.model_copy(update={"num_passes": 2})}
    )
    runner = PipelineRunner.from_config(cfg)
    runner._bus._sinks.append(_WedgedSink())
    runner._bus._join_timeout_s = 0.5
    try:
        with pytest.raises(RuntimeError, match="drain"):
            runner.run()
        assert runner._bus.events_lost >= 1  # the queued tail is counted
    finally:
        release.set()


def test_dropping_queue_carries_discontinuity_past_drops() -> None:
    """Design-review fix for finding 2: drop-oldest is premised on frame
    redundancy, but the one marked frame is not redundant — a dropped
    break signal must ride on the next frame handed out."""
    import numpy as np

    from palletscan.reliability.queues import DroppingQueue
    from palletscan.types import Frame

    img = np.zeros((2, 2), np.uint8)

    def fr(i: int, disc: bool = False) -> Frame:
        return Frame(
            image=img, ts=i / 30.0, frame_index=i, source_id="c", discontinuity=disc
        )

    q = DroppingQueue(maxsize=2)
    q.put(fr(0, disc=True))
    q.put(fr(1))
    q.put(fr(2))  # drops frame 0 — the marked one
    assert q.dropped == 1
    first = q.get()
    assert first.frame_index == 1
    assert first.discontinuity is True, "the break signal was dropped"
    second = q.get()
    assert second.discontinuity is False, "the carry-over must be one-shot"


def test_ts_jump_finalizes_misses_before_buffer_eviction(tmp_path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding b4 (repro: the post-reconnect,
    ts-jumped frame entered the rolling buffer BEFORE the tracker's
    deadline hook ran, so horizon eviction discarded the already-captured
    post-roll frames one call before _finalize_miss harvested them — the
    MissEvent stored but its burst silently lacked the exit-side frames)."""
    import numpy as np
    from pathlib import Path

    from palletscan.types import Frame, MissEvent

    runner = _runner_for_manual_drive(tmp_path)
    runner._bus.start()
    fps = 30.0

    def frame(i: int, ts: float, moving: bool) -> Frame:
        img = np.full((240, 480), 128, np.uint8)
        if moving:
            x = 30 + (i % 20) * 15
            img[60:180, x : x + 100] = 230
        return Frame(image=img, ts=ts, frame_index=i, source_id="cam0")

    for i in range(0, 6):
        runner._process_frame(frame(i, i / fps, moving=False))
    for i in range(6, 26):
        runner._process_frame(frame(i, i / fps, moving=True))
    # quiet frames close the segment (backdated to ~25); stop just short
    # of the post-roll deadline so the miss is still pending.
    for i in range(26, 80):
        runner._process_frame(frame(i, i / fps, moving=False))
    assert runner._tracker.misses_emitted == 0, "miss must still be pending"
    # The ts jump that finalizes it would have evicted the post-roll first.
    runner._process_frame(frame(80, 80 / fps + 300.0, moving=False))
    runner._bus.shutdown()
    runner._executor.shutdown(wait=True)

    misses = [e for e in runner.collected_events if isinstance(e, MissEvent)]
    assert len(misses) == 1
    miss = misses[0]
    jpg_indices = sorted(
        int(p.stem.split("_")[1])
        for p in Path(miss.evidence_dir).glob("*.jpg")
    )
    assert jpg_indices, "the burst must hold frames"
    # Discriminating depth assertion (closure-check fix): the reservoir
    # already holds quiet-gap frames slightly past last_frame (~33 here),
    # so a shallow `> last_frame` check passes under the BROKEN ordering
    # too. Only the deadline-before-eviction order preserves the deep
    # post-roll (frames fed up to ~79, ts well past close_ts + post_s/2).
    assert jpg_indices[-1] >= 60, (
        f"the burst lacks the post-roll (exit-side) frames (max index "
        f"{jpg_indices[-1]}): eviction ran before the deadline harvest "
        "(finding b4)"
    )
