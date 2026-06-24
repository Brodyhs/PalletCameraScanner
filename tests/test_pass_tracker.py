"""PassTracker: aggregation, dedup window, miss path with evidence."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from palletscan.config import BufferConfig, DedupConfig, EvidenceConfig
from palletscan.events.evidence import EvidenceWriter
from palletscan.pipeline.pass_tracker import PassTracker
from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.types import (
    DecodeResult,
    Frame,
    MissEvent,
    PassEvent,
    Roi,
    SegmentEvent,
    SegmentKind,
    Symbology,
)

_IMG = np.zeros((20, 20), np.uint8)
FPS = 30.0


def _decode(payload: str, frame_index: int) -> DecodeResult:
    return DecodeResult(
        payload=payload,
        symbology=Symbology.QR,
        roi=Roi(0, 0, 10, 10),
        frame_index=frame_index,
        ts=frame_index / FPS,
        source_id="cam0",
        decoder="pyzbar",
        latency_ms=1.0,
    )


def _seg(kind: SegmentKind, cid: str, frame_index: int) -> SegmentEvent:
    return SegmentEvent(
        kind=kind, candidate_id=cid, frame_index=frame_index, ts=frame_index / FPS
    )


@pytest.fixture()
def setup(tmp_path: Path):
    events: list = []
    buffer = RollingFrameBuffer(horizon_s=5.0)
    tracker = PassTracker(
        dedup_cfg=DedupConfig(window_s=12.0),
        buffer_cfg=BufferConfig(pre_s=2.0, post_s=2.0),
        evidence=EvidenceWriter(EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)),
        buffer=buffer,
        emit=events.append,
        source_id="cam0",
    )
    return tracker, buffer, events


def _feed_frames(tracker: PassTracker, buffer: RollingFrameBuffer, rng: range) -> None:
    for i in rng:
        f = Frame(image=_IMG, ts=i / FPS, frame_index=i, source_id="cam0")
        buffer.append(f)
        tracker.on_frame(f)


def test_many_decodes_one_pass_event(setup) -> None:
    tracker, buffer, events = setup
    _feed_frames(tracker, buffer, range(0, 10))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    for i in (11, 13, 15):
        tracker.on_decode("cam0-000001", [_decode("PLT-000001", i)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, PassEvent)
    assert ev.payload == "PLT-000001"
    assert ev.decode_count == 3
    assert ev.cameras == {"cam0": 3}
    assert ev.first_seen_ts == pytest.approx(10 / FPS)
    assert ev.last_seen_ts == pytest.approx(20 / FPS)
    assert ev.best_frame == ("cam0", 11)
    # Phase 4 additive fields: first decode timing + per-camera detail.
    assert ev.first_decode_ts == pytest.approx(11 / FPS)
    assert ev.revision == 0
    assert set(ev.camera_detail) == set(ev.cameras) == {"cam0"}
    detail = ev.camera_detail["cam0"]
    assert detail["first_seen_ts"] == pytest.approx(ev.first_seen_ts)
    assert detail["first_decode_ts"] == pytest.approx(11 / FPS)
    assert detail["last_seen_ts"] == pytest.approx(ev.last_seen_ts)
    assert detail["decode_count"] == ev.decode_count == 3


def test_confirmed_set_on_first_decode(setup) -> None:
    tracker, _, _ = setup
    ctx = tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 5))
    assert not ctx.confirmed
    tracker.on_decode("cam0-000001", [_decode("PLT-000002", 6)])
    assert ctx.confirmed


def test_same_payload_within_window_merges(setup) -> None:
    tracker, buffer, events = setup
    # first sighting closes at frame 20 (t=0.67s)
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode("cam0-000001", [_decode("PLT-000003", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    # second sighting ~5 s later -> merged
    base = 20 + int(5 * FPS)
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", base))
    tracker.on_decode("cam0-000002", [_decode("PLT-000003", base + 2)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", base + 10))
    assert len([e for e in events if isinstance(e, PassEvent)]) == 1
    assert tracker.passes_merged == 1


def test_same_payload_after_window_is_new_pass(setup) -> None:
    tracker, buffer, events = setup
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode("cam0-000001", [_decode("PLT-000004", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    base = 20 + int(13 * FPS)  # 13 s later, > 12 s window
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", base))
    tracker.on_decode("cam0-000002", [_decode("PLT-000004", base + 2)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", base + 10))
    assert len([e for e in events if isinstance(e, PassEvent)]) == 2


def test_zero_decode_segment_becomes_miss_with_evidence(setup) -> None:
    tracker, buffer, events = setup
    _feed_frames(tracker, buffer, range(0, 30))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 30))
    _feed_frames(tracker, buffer, range(30, 50))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 50))
    # miss is NOT finalized until the post-roll deadline passes
    assert events == []
    post_frames = int(2.0 * FPS) + 2
    _feed_frames(tracker, buffer, range(50, 50 + post_frames))
    assert len(events) == 1
    miss = events[0]
    assert isinstance(miss, MissEvent)
    assert miss.candidate_id == "cam0-000001"
    evidence_dir = Path(miss.evidence_dir)
    assert evidence_dir.is_dir()
    assert list(evidence_dir.glob("*.jpg"))
    assert (evidence_dir / "meta.json").exists()
    # burst includes pre-roll and post-roll frames around the segment
    indices = sorted(
        int(p.stem.split("_")[1]) for p in evidence_dir.glob("*.jpg")
    )
    assert indices[0] < 30
    assert indices[-1] > 50


def test_flush_finalizes_pending_misses(setup) -> None:
    tracker, buffer, events = setup
    _feed_frames(tracker, buffer, range(0, 40))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 40))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 45))
    assert events == []
    tracker.flush()
    assert len(events) == 1
    assert isinstance(events[0], MissEvent)


def test_flush_closes_open_decoded_segment(setup) -> None:
    tracker, buffer, events = setup
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode("cam0-000001", [_decode("PLT-000009", 12)])
    tracker.flush()
    assert len(events) == 1
    assert isinstance(events[0], PassEvent)
    assert events[0].payload == "PLT-000009"


class _ExplodingWriter:
    """Evidence writer whose write_burst always raises (non-OSError, so it
    exercises the tracker's belt-and-braces layer, not the writer's own
    OSError tolerance)."""

    def __init__(self) -> None:
        self.calls = 0

    def write_burst(self, candidate_id, frames, meta):
        self.calls += 1
        raise RuntimeError("simulated evidence failure")


class _RecordingWriter:
    """Captures the frames handed to write_burst without touching disk."""

    def __init__(self) -> None:
        self.bursts: list[list[Frame]] = []

    def write_burst(self, candidate_id, frames, meta):
        from palletscan.events.evidence import EvidenceRef

        self.bursts.append(list(frames))
        return EvidenceRef(directory=Path("/dev/null"), frame_count=len(frames))


def _tracker_with_writer(writer, ts_to_wall=None):
    events: list = []
    buffer = RollingFrameBuffer(horizon_s=5.0)
    tracker = PassTracker(
        dedup_cfg=DedupConfig(window_s=12.0),
        buffer_cfg=BufferConfig(pre_s=2.0, post_s=2.0),
        evidence=writer,
        buffer=buffer,
        emit=events.append,
        source_id="cam0",
        ts_to_wall=ts_to_wall,
    )
    return tracker, buffer, events


def test_evidence_write_failure_still_emits_flagged_miss() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 1 (reproduced there): the pending miss
    was destructively popped before the emit, so a raising evidence write
    swallowed the MissEvent permanently — even a one-shot transient fault
    recovered nothing. The miss must emit evidence-less and flagged."""
    tracker, buffer, events = _tracker_with_writer(_ExplodingWriter())
    _feed_frames(tracker, buffer, range(0, 30))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 30))
    _feed_frames(tracker, buffer, range(30, 50))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 50))
    _feed_frames(tracker, buffer, range(50, 50 + int(2.0 * FPS) + 2))
    assert len(events) == 1
    miss = events[0]
    assert isinstance(miss, MissEvent)
    assert miss.evidence_error is not None
    assert miss.evidence_dir == ""
    assert miss.evidence_frame_count == 0
    assert tracker.misses_emitted == 1
    assert tracker.evidence_failures == 1


def test_flush_drain_survives_evidence_failures() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 1, shutdown variant: the same raise
    aborted the flush drain loop, discarding ALL remaining pending misses
    at once. Every pending miss must emit."""
    tracker, buffer, events = _tracker_with_writer(_ExplodingWriter())
    for n, (o, c) in enumerate([(10, 20), (40, 50), (70, 80)], start=1):
        cid = f"cam0-{n:06d}"
        tracker.on_segment_open(_seg(SegmentKind.OPEN, cid, o))
        tracker.on_segment_close(_seg(SegmentKind.CLOSE, cid, c))
    tracker.flush()
    misses = [e for e in events if isinstance(e, MissEvent)]
    assert len(misses) == 3
    assert all(m.evidence_error is not None for m in misses)
    assert tracker.evidence_failures == 3


def test_post_roll_excludes_frames_already_in_reservoir() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b11 (reproduced there): quiet-gap
    frames (ts > close_ts, observed while the segment wound down) sat in
    BOTH miss.frames and the post-roll re-extract, double-writing the same
    frame and overstating evidence_frame_count."""
    writer = _RecordingWriter()
    tracker, buffer, _ = _tracker_with_writer(writer)
    # Segment closes backdated to frame 40 (last active); frames 41..49
    # are the quiet gap: ts > close_ts AND captured by on_frame while the
    # segment was still open AND still in the rolling buffer at finalize.
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 30))
    _feed_frames(tracker, buffer, range(30, 50))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 40))
    _feed_frames(tracker, buffer, range(50, 50 + int(2.0 * FPS) + 2))
    assert len(writer.bursts) == 1
    indices = [f.frame_index for f in writer.bursts[0]]
    assert len(indices) == len(set(indices)), (
        f"burst double-includes frames: {sorted(indices)}"
    )


def test_deferred_miss_wall_time_is_close_time_not_finalize_time() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b12 (reproduced there): wall_time_iso
    was stamped at deferred finalize time, attributing an outage-deferred
    miss to the reconnect's report window instead of the window where the
    pallet passed."""
    from palletscan.types import iso_at

    writer = _RecordingWriter()
    tracker, buffer, events = _tracker_with_writer(
        writer, ts_to_wall=lambda ts: iso_at(1000.0 + ts)
    )
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 120))
    _feed_frames(tracker, buffer, range(120, 150))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 150))
    # Outage: the next frame arrives 115 s later (source clock), far past
    # the post-roll deadline — the finalize is deferred until now.
    late = Frame(
        image=_IMG, ts=150 / FPS + 115.0, frame_index=151, source_id="cam0"
    )
    buffer.append(late)
    tracker.on_frame(late)
    assert len(events) == 1
    miss = events[0]
    assert isinstance(miss, MissEvent)
    assert miss.wall_time_iso == iso_at(1000.0 + 150 / FPS)


def test_pass_wall_time_uses_close_ts_mapping() -> None:
    """Finding b12, pass side: PassEvent wall stamps go through the same
    close-ts mapping so windowed A/B reports bucket consistently."""
    from palletscan.types import iso_at

    tracker, buffer, events = _tracker_with_writer(
        _RecordingWriter(), ts_to_wall=lambda ts: iso_at(2000.0 + ts)
    )
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode("cam0-000001", [_decode("PLT-000020", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    assert len(events) == 1
    assert events[0].wall_time_iso == iso_at(2000.0 + 20 / FPS)


def test_seed_recent_bridges_restart_window() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 10, tracker half: the payload window
    seeded from the previous run's stored passes (anchors mapped into this
    process's clock, negative) merges a restart-spanning re-sighting
    instead of double-counting it; beyond the window it emits."""
    tracker, buffer, events = _tracker_with_writer(_RecordingWriter())
    tracker.seed_recent({"PLT-RESTART": -5.0})
    # Re-sighting closing at ts 5.0: 10 s since the stored pass <= 12.
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 148))
    tracker.on_decode("cam0-000001", [_decode("PLT-RESTART", 149)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 150))
    assert events == []
    assert tracker.passes_merged == 1
    # A different payload is unaffected.
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", 160))
    tracker.on_decode("cam0-000002", [_decode("PLT-FRESH", 161)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", 170))
    assert [e.payload for e in events if isinstance(e, PassEvent)] == ["PLT-FRESH"]


def test_seed_recent_beyond_window_emits() -> None:
    """Finding 10 control: a sighting beyond the window of the seeded
    anchor is a genuine new business pass."""
    tracker, buffer, events = _tracker_with_writer(_RecordingWriter())
    tracker.seed_recent({"PLT-RESTART": -5.0})
    close = int(8.0 * FPS)  # close_ts 8.0: 13 s since the stored pass > 12
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", close - 10))
    tracker.on_decode("cam0-000001", [_decode("PLT-RESTART", close - 5)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", close))
    assert len([e for e in events if isinstance(e, PassEvent)]) == 1
    assert tracker.passes_merged == 0


def test_two_payloads_in_one_segment_emit_two_events(setup) -> None:
    tracker, buffer, events = setup
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode("cam0-000001", [_decode("PLT-000010", 11)])
    tracker.on_decode("cam0-000001", [_decode("PLT-000011", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    payloads = {e.payload for e in events if isinstance(e, PassEvent)}
    assert payloads == {"PLT-000010", "PLT-000011"}


# -- TIER 2: concurrent segments (multi-object tracking) ----------------------


def test_concurrent_segments_decode_independently(setup) -> None:
    """Two segments open at once; decodes route to the right one by
    candidate_id, yielding two distinct PassEvents."""
    tracker, buffer, events = setup
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", 11))
    tracker.on_decode("cam0-000001", [_decode("PLT-AAA", 12)])
    tracker.on_decode("cam0-000002", [_decode("PLT-BBB", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", 21))
    passes = [e for e in events if isinstance(e, PassEvent)]
    assert {p.payload for p in passes} == {"PLT-AAA", "PLT-BBB"}
    by_payload = {p.payload: p for p in passes}
    assert by_payload["PLT-AAA"].candidate_ids == ["cam0-000001"]
    assert by_payload["PLT-BBB"].candidate_ids == ["cam0-000002"]


def test_one_concurrent_segment_misses_while_other_passes(setup) -> None:
    """The core account-for-everything win: A decodes, B does not. Exactly
    one PassEvent(A) and one MissEvent(B), with DISTINCT evidence dirs — a
    decoded pallet can no longer swallow a co-located undecoded one's miss."""
    tracker, buffer, events = setup
    _feed_frames(tracker, buffer, range(0, 30))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 30))  # A
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", 30))  # B
    _feed_frames(tracker, buffer, range(30, 50))
    tracker.on_decode("cam0-000001", [_decode("PLT-PASS", 35)])  # only A decodes
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 50))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", 50))
    # A emits immediately; B's miss waits out the post-roll deadline.
    _feed_frames(tracker, buffer, range(50, 50 + int(2.0 * FPS) + 2))
    passes = [e for e in events if isinstance(e, PassEvent)]
    misses = [e for e in events if isinstance(e, MissEvent)]
    assert len(passes) == 1 and passes[0].payload == "PLT-PASS"
    assert len(misses) == 1 and misses[0].candidate_id == "cam0-000002"
    # Distinct evidence directories (separate reservoirs).
    assert Path(misses[0].evidence_dir).is_dir()
    assert misses[0].candidate_id not in passes[0].candidate_ids


def test_on_frame_feeds_all_open_reservoirs(tmp_path: Path) -> None:
    """on_frame feeds EVERY open segment's reservoir, so two concurrent misses
    each get their own non-empty evidence burst.

    The reservoir is the ONLY frame source here: pre-roll is empty (the
    segments open before any frame is buffered) and post-roll is disabled
    (``post_s=0`` -> the post re-extract window ``ts > close_ts`` is empty). So
    a non-empty burst REQUIRES the per-segment reservoir feed in ``on_frame``;
    if that feed were a no-op the bursts would be empty and this fails. (The
    weaker version of this test passed even with the feed disabled, because a
    2 s post-roll backfilled the segment window from the shared buffer.)"""
    events: list = []
    buffer = RollingFrameBuffer(horizon_s=5.0)
    tracker = PassTracker(
        dedup_cfg=DedupConfig(window_s=12.0),
        # pre_s=0: even the open-time snapshot extracts nothing (and the buffer
        # is empty at open anyway). post_s=0: no post-roll backfill.
        buffer_cfg=BufferConfig(pre_s=0.0, post_s=0.0),
        evidence=EvidenceWriter(EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)),
        buffer=buffer,
        emit=events.append,
        source_id="cam0",
    )
    # Open BOTH segments with an empty buffer -> no pre-roll snapshot.
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 20))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", 20))
    _feed_frames(tracker, buffer, range(20, 40))  # ONLY the reservoir feed here
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 40))
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", 40))
    # post_s=0 -> deadline == close_ts; the next frame's clock tick finalizes.
    _feed_frames(tracker, buffer, range(40, 45))
    misses = [e for e in events if isinstance(e, MissEvent)]
    assert len(misses) == 2
    for m in misses:
        d = Path(m.evidence_dir)
        assert d.is_dir()
        # Frames here can ONLY have come from the reservoir feed.
        assert list(d.glob("*.jpg")), f"{m.candidate_id} got an empty burst"
        assert m.evidence_frame_count > 0, m.candidate_id
