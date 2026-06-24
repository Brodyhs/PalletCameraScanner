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

from pathlib import Path

import numpy as np

from palletscan.config import (
    BufferConfig,
    DedupConfig,
    EvidenceConfig,
    MotionConfig,
)
from palletscan.events.evidence import EvidenceWriter
from palletscan.pipeline.motion_gate import MotionGate
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
