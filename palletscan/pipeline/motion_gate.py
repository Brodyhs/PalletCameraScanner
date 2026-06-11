"""MotionGate: cheap motion detection that gates the expensive decode work.

Runs on heavily downscaled grayscale (INTER_AREA averaging also crushes
per-pixel sensor noise), maintains a pass-candidate segment state machine
(debounced open, quiet-frame close), and emits a full-resolution ROI around
the moving region.
"""

from __future__ import annotations

import cv2
import numpy as np

from palletscan.config import MotionAlgorithm, MotionConfig
from palletscan.types import Frame, MotionResult, Roi, SegmentEvent, SegmentKind


class MotionGate:
    """Per-source motion gate. Call :meth:`update` once per frame in order."""

    def __init__(self, cfg: MotionConfig, source_id: str) -> None:
        self._cfg = cfg
        self._source_id = source_id
        self._prev_small: np.ndarray | None = None
        self._mog2 = (
            cv2.createBackgroundSubtractorMOG2(detectShadows=False)
            if cfg.algorithm is MotionAlgorithm.MOG2
            else None
        )
        self._mog2_primed = False
        self._kernel = np.ones((3, 3), np.uint8)
        # Segment state
        self._segment_count = 0
        self._active_streak = 0
        self._quiet_streak = 0
        self._open_id: str | None = None
        self._open_backdate: tuple[int, float] | None = None  # first active frame
        self._last_active: tuple[int, float] | None = None

    def _mask(self, small: np.ndarray) -> np.ndarray | None:
        """Binary motion mask on the downscaled frame, or None on warm-up."""
        if self._mog2 is not None:
            raw = self._mog2.apply(small)
            if not self._mog2_primed:
                # MOG2 has no background model yet on the first frame and
                # reports the entire frame as foreground.
                self._mog2_primed = True
                return None
            return cv2.dilate((raw > 0).astype(np.uint8), self._kernel)
        prev, self._prev_small = self._prev_small, small
        if prev is None:
            return None
        diff = cv2.absdiff(small, prev)
        mask = (diff > self._cfg.diff_threshold).astype(np.uint8)
        return cv2.dilate(mask, self._kernel)

    def update(self, frame: Frame) -> tuple[MotionResult, SegmentEvent | None]:
        """Classify one frame; possibly emit a segment open/close event."""
        cfg = self._cfg
        h, w = frame.image.shape
        sw = cfg.downscale_width
        sh = max(1, round(h * sw / w))
        small = cv2.resize(frame.image, (sw, sh), interpolation=cv2.INTER_AREA)
        mask = self._mask(small)

        roi: Roi | None = None
        motion_frac = 0.0
        if mask is not None:
            motion_frac = float(np.count_nonzero(mask)) / mask.size
            # The > 0 guard keeps min_area_frac: 0 from reducing an empty
            # mask (np.nonzero -> empty arrays -> min() raises).
            if motion_frac >= cfg.min_area_frac and motion_frac > 0.0:
                ys, xs = np.nonzero(mask)
                scale = w / sw
                roi = Roi(
                    x=int(xs.min() * scale),
                    y=int(ys.min() * scale),
                    w=int((xs.max() + 1 - xs.min()) * scale),
                    h=int((ys.max() + 1 - ys.min()) * scale),
                ).pad(cfg.roi_pad_px).clamp(frame.image.shape)

        active = roi is not None
        event: SegmentEvent | None = None
        if active:
            self._quiet_streak = 0
            self._active_streak += 1
            if self._active_streak == 1:
                self._open_backdate = (frame.frame_index, frame.ts)
            self._last_active = (frame.frame_index, frame.ts)
            if self._open_id is None and self._active_streak >= cfg.open_frames:
                self._segment_count += 1
                self._open_id = f"{self._source_id}-{self._segment_count:06d}"
                backdate = self._open_backdate or (frame.frame_index, frame.ts)
                event = SegmentEvent(
                    kind=SegmentKind.OPEN,
                    candidate_id=self._open_id,
                    frame_index=backdate[0],
                    ts=backdate[1],
                )
        else:
            self._active_streak = 0
            if self._open_id is not None:
                self._quiet_streak += 1
                if self._quiet_streak >= cfg.quiet_frames:
                    event = self._close_segment()

        return (
            MotionResult(
                active=active,
                candidate_id=self._open_id,
                roi=roi,
                motion_frac=motion_frac,
            ),
            event,
        )

    def _close_segment(self) -> SegmentEvent:
        assert self._open_id is not None and self._last_active is not None
        event = SegmentEvent(
            kind=SegmentKind.CLOSE,
            candidate_id=self._open_id,
            frame_index=self._last_active[0],
            ts=self._last_active[1],
        )
        self._open_id = None
        self._quiet_streak = 0
        self._active_streak = 0
        return event

    def flush(self) -> SegmentEvent | None:
        """Close any open segment at end-of-stream."""
        if self._open_id is not None:
            return self._close_segment()
        return None
