"""LivePreview: per-runner latest-frame tap rendered as overlay JPEGs.

The pipeline thread calls :meth:`update` once per frame (a reference swap
plus deque bookkeeping under a lock — no encoding on the hot path). Any
number of MJPEG clients call :meth:`render_jpeg`, which copies the shared
references under the lock and does all drawing/encoding outside it.

Decode boxes linger for about a second of *source time* after the decode,
so a one-frame hit stays visible to the eye at preview rates. Memory is
bounded by construction: one frame reference plus a small overlay deque.
"""

from __future__ import annotations

import threading
from collections import deque

import cv2
import numpy as np

from palletscan.config import WebConfig
from palletscan.types import DecodeResult, Frame, MotionResult

#: Source-time seconds a decode overlay stays visible after its frame.
_DECODE_LINGER_S = 1.0

#: Hard cap on lingering decode overlays (a pallet face yields a handful).
_MAX_OVERLAYS = 32

_MOTION_COLOR = (80, 200, 255)  # BGR: amber-ish for motion candidates
_DECODE_COLOR = (80, 255, 80)  # BGR: green for confirmed decodes
_TEXT_COLOR = (255, 255, 255)


class LivePreview:
    """Latest frame + overlay state for one camera's live view."""

    def __init__(self, source_id: str, cfg: WebConfig) -> None:
        self.source_id = source_id
        self._cfg = cfg
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._motion: MotionResult | None = None
        self._decodes: deque[DecodeResult] = deque(maxlen=_MAX_OVERLAYS)
        self._stamp = 0

    def update(
        self, frame: Frame, motion: MotionResult, decodes: list[DecodeResult]
    ) -> None:
        """Pipeline-thread hook: swap in the newest frame state."""
        with self._lock:
            self._frame = frame
            self._motion = motion
            self._decodes.extend(decodes)
            cutoff = frame.ts - _DECODE_LINGER_S
            while self._decodes and self._decodes[0].ts < cutoff:
                self._decodes.popleft()
            self._stamp += 1

    @property
    def stamp(self) -> int:
        """Monotone update counter; MJPEG streams yield on change."""
        with self._lock:
            return self._stamp

    def render_jpeg(self) -> tuple[bytes | None, int]:
        """Encode the latest frame with overlays; ``(None, stamp)`` before
        the first frame arrives."""
        with self._lock:
            frame = self._frame
            motion = self._motion
            decodes = list(self._decodes)
            stamp = self._stamp
        if frame is None:
            return None, stamp
        # All drawing happens on a private BGR copy outside the lock. Apply the
        # cosmetic preview brightness boost to the camera image FIRST so overlays
        # drawn on top stay vivid (view-only; capture/decode are unaffected).
        base = frame.image
        if self._cfg.preview_gain != 1.0:
            base = cv2.convertScaleAbs(base, alpha=self._cfg.preview_gain)
        image = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
        if motion is not None and motion.tracks:
            # Multi-object mode: one amber box + small track-id label per track.
            for track in motion.tracks:
                roi = track.roi.clamp(frame.image.shape)
                cv2.rectangle(
                    image,
                    (roi.x, roi.y),
                    (roi.x + roi.w, roi.y + roi.h),
                    _MOTION_COLOR,
                    2,
                )
                cv2.putText(
                    image,
                    track.track_id,
                    (roi.x, max(14, roi.y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    _MOTION_COLOR,
                    1,
                )
        elif motion is not None and motion.active and motion.roi is not None:
            roi = motion.roi.clamp(frame.image.shape)
            cv2.rectangle(
                image,
                (roi.x, roi.y),
                (roi.x + roi.w, roi.y + roi.h),
                _MOTION_COLOR,
                2,
            )
        for decode in decodes:
            roi = decode.roi.clamp(frame.image.shape)
            cv2.rectangle(
                image,
                (roi.x, roi.y),
                (roi.x + roi.w, roi.y + roi.h),
                _DECODE_COLOR,
                3,
            )
            cv2.putText(
                image,
                decode.payload,
                (roi.x, max(20, roi.y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                _DECODE_COLOR,
                2,
            )
        header = (
            f"{frame.source_id}  f={frame.frame_index}  t={frame.ts:.2f}s"
            + ("  MOTION" if motion is not None and motion.active else "")
        )
        cv2.putText(
            image, header, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, _TEXT_COLOR, 2
        )
        width = self._cfg.preview_width
        if image.shape[1] > width:
            scale = width / image.shape[1]
            image = cv2.resize(
                image,
                (width, max(1, int(round(image.shape[0] * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        ok, buf = cv2.imencode(
            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self._cfg.preview_quality]
        )
        if not ok:  # pragma: no cover - encoder failure
            return None, stamp
        data: np.ndarray = buf
        return data.tobytes(), stamp
