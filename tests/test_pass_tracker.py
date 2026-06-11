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
        tracker.on_frame_ts(f.ts)


def test_many_decodes_one_pass_event(setup) -> None:
    tracker, buffer, events = setup
    _feed_frames(tracker, buffer, range(0, 10))
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    for i in (11, 13, 15):
        tracker.on_decode([_decode("PLT-000001", i)])
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


def test_confirmed_set_on_first_decode(setup) -> None:
    tracker, _, _ = setup
    ctx = tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 5))
    assert not ctx.confirmed
    tracker.on_decode([_decode("PLT-000002", 6)])
    assert ctx.confirmed


def test_same_payload_within_window_merges(setup) -> None:
    tracker, buffer, events = setup
    # first sighting closes at frame 20 (t=0.67s)
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode([_decode("PLT-000003", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    # second sighting ~5 s later -> merged
    base = 20 + int(5 * FPS)
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", base))
    tracker.on_decode([_decode("PLT-000003", base + 2)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000002", base + 10))
    assert len([e for e in events if isinstance(e, PassEvent)]) == 1
    assert tracker.passes_merged == 1


def test_same_payload_after_window_is_new_pass(setup) -> None:
    tracker, buffer, events = setup
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode([_decode("PLT-000004", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    base = 20 + int(13 * FPS)  # 13 s later, > 12 s window
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000002", base))
    tracker.on_decode([_decode("PLT-000004", base + 2)])
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
    tracker.on_decode([_decode("PLT-000009", 12)])
    tracker.flush()
    assert len(events) == 1
    assert isinstance(events[0], PassEvent)
    assert events[0].payload == "PLT-000009"


def test_two_payloads_in_one_segment_emit_two_events(setup) -> None:
    tracker, buffer, events = setup
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-000001", 10))
    tracker.on_decode([_decode("PLT-000010", 11)])
    tracker.on_decode([_decode("PLT-000011", 12)])
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-000001", 20))
    payloads = {e.payload for e in events if isinstance(e, PassEvent)}
    assert payloads == {"PLT-000010", "PLT-000011"}
