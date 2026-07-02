"""Recording tap: ``recording.post_s`` genuinely controls a record's post-roll.

Patch-fixer coverage for the post-roll defect: ``_PendingRecord.deadline_ts``
used ``record_post_s`` but the shared ``_harvest_post`` hardcoded the extract
window to ``buffer.post_s``, so raising ``recording.post_s`` delayed the write
without capturing any more trailing frames (and beyond the rolling-buffer
horizon captured ZERO). The window is now the record's own ``record_post_s``,
and the buffer horizon (app.py) is sized to retain it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from palletscan.app import PipelineRunner
from palletscan.config import (
    AppConfig,
    BufferConfig,
    DedupConfig,
    EvidenceConfig,
    RecordingConfig,
    apply_overrides,
)
from palletscan.events.evidence import EvidenceWriter
from palletscan.pipeline.pass_tracker import PassTracker
from palletscan.pipeline.rolling_buffer import RollingFrameBuffer
from palletscan.pipeline.segment_recorder import SegmentRecorder
from palletscan.sources.base import FrameSource
from palletscan.types import Frame, SegmentEvent, SegmentKind

_IMG = np.zeros((8, 8), np.uint8)
FPS = 10.0
_CLOSE_FRAME = 50
_CLOSE_TS = _CLOSE_FRAME / FPS  # 5.0s


def _frame(i: int) -> Frame:
    return Frame(image=_IMG, ts=i / FPS, frame_index=i, source_id="cam0")


def _seg(kind: SegmentKind, cid: str, i: int) -> SegmentEvent:
    return SegmentEvent(kind=kind, candidate_id=cid, frame_index=i, ts=i / FPS)


def _record_postroll(tmp_path: Path, record_post_s: float) -> list[Frame]:
    """Run one undecoded (miss) segment through the tracker with recording on,
    and return the post-roll frames (ts > close) of the queued record burst.

    The recorder thread is intentionally NOT started, so the submitted burst
    stays in its queue for inspection. A generous rolling-buffer horizon
    isolates the ``_harvest_post`` window fix from the app-level horizon fix.
    """
    rec = SegmentRecorder(
        RecordingConfig(
            enabled=True,
            post_s=record_post_s,
            evidence=EvidenceConfig(dir=tmp_path / "rec", frame_stride=1),
        )
    )
    buffer = RollingFrameBuffer(horizon_s=60.0, maxlen=100_000)
    tracker = PassTracker(
        dedup_cfg=DedupConfig(window_s=12.0),
        buffer_cfg=BufferConfig(pre_s=2.0, post_s=2.0),
        evidence=EvidenceWriter(EvidenceConfig(dir=tmp_path / "miss", frame_stride=1)),
        buffer=buffer,
        emit=lambda ev: None,
        source_id="cam0",
        recorder=rec,
        record_post_s=record_post_s,
    )

    def feed(i: int) -> None:
        f = _frame(i)
        buffer.append(f)
        tracker.on_frame(f)

    for i in range(0, 30):  # pre-roll
        feed(i)
    tracker.on_segment_open(_seg(SegmentKind.OPEN, "cam0-1", 30))
    for i in range(30, _CLOSE_FRAME + 1):  # in-segment, no decode -> miss
        feed(i)
    tracker.on_segment_close(_seg(SegmentKind.CLOSE, "cam0-1", _CLOSE_FRAME))
    for i in range(_CLOSE_FRAME + 1, 200):  # post-roll -> drains the record
        feed(i)

    job = rec.queue.get_nowait()  # exactly one record queued for this segment
    assert rec.queue.empty()
    return [f for f in job.frames if f.ts > _CLOSE_TS]


def test_record_post_roll_scales_with_recording_post_s(tmp_path: Path) -> None:
    """The discriminating test: at 10 fps the record's post-roll frame count
    tracks ``recording.post_s`` (2s -> 20, 4s -> 40, 10s -> 100). Before the
    fix all three were pinned to buffer.post_s (20), and 10s (past the old
    horizon) collapsed to 0."""
    p2 = _record_postroll(tmp_path / "a", 2.0)
    p4 = _record_postroll(tmp_path / "b", 4.0)
    p10 = _record_postroll(tmp_path / "c", 10.0)

    assert len(p2) == 20
    assert len(p4) == 40
    assert len(p10) == 100
    # Strictly monotonic: raising the knob captures MORE trailing evidence.
    assert len(p4) > len(p2) > 0
    assert len(p10) > len(p4)
    # Contiguous, not a gap: the first post-roll frame is the one right after
    # close (guards against the beyond-horizon eviction regression).
    assert p10[0].frame_index == _CLOSE_FRAME + 1


def test_record_post_roll_matches_buffer_at_default(tmp_path: Path) -> None:
    """Byte-identical default: recording.post_s == buffer.post_s == 2.0 yields
    the same 2s post-roll the miss path harvests."""
    assert len(_record_postroll(tmp_path, 2.0)) == 20


# -- app-level rolling-buffer horizon (companion to the _harvest_post fix) -----


class _ManualSource(FrameSource):
    """Minimal FrameSource stand-in; never actually iterated here."""

    @property
    def source_id(self) -> str:
        return "cam0"

    @property
    def nominal_fps(self) -> float | None:
        return 30.0

    def frames(self) -> Iterator[Frame]:  # pragma: no cover - not driven here
        return iter(())


def test_buffer_horizon_covers_recording_post_s_when_enabled(tmp_path: Path) -> None:
    base = apply_overrides(AppConfig(), data_dir=tmp_path)
    rec_cfg = base.recording.model_copy(update={"enabled": True, "post_s": 10.0})
    cfg = base.model_copy(update={"recording": rec_cfg})
    runner = PipelineRunner(cfg, _ManualSource(), sinks=[])
    # A large recording post-roll must widen the horizon, else the record
    # would harvest evicted (empty) frames.
    assert runner._buffer._horizon_s == pytest.approx(
        cfg.buffer.pre_s + cfg.recording.post_s + 1.0
    )
    assert runner._buffer._horizon_s >= cfg.recording.post_s


def test_buffer_horizon_unchanged_when_recording_disabled(tmp_path: Path) -> None:
    cfg = apply_overrides(AppConfig(), data_dir=tmp_path)
    assert cfg.recording.enabled is False
    runner = PipelineRunner(cfg, _ManualSource(), sinks=[])
    # Default OFF: the horizon is byte-identical to the pre-patch formula.
    assert runner._buffer._horizon_s == pytest.approx(
        cfg.buffer.pre_s + cfg.buffer.post_s + 1.0
    )
