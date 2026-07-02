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
        #: Encode-on-change cache: (stamp, jpeg bytes). The MJPEG poll re-calls
        #: render_jpeg every ~100ms and, when idle, the keepalive re-polls the
        #: SAME frame — reuse the last encode instead of re-doing it.
        self._cache: tuple[int, bytes] | None = None

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
        the first frame arrives.

        Downscale FIRST, then convert/draw/encode on the small image: a mono
        2064x1552 GRAY->BGR + full-res overlay draw + full-res INTER_AREA is ~2x
        the small-image path and competes with the pipeline thread for CPU (the
        source of the laggy mono live view). Overlay coordinates scale by the
        same factor. Results are cached per stamp so an idle keepalive re-poll
        of the same frame does not re-encode."""
        with self._lock:
            frame = self._frame
            motion = self._motion
            decodes = list(self._decodes)
            stamp = self._stamp
            cached = self._cache
        if frame is None:
            return None, stamp
        if cached is not None and cached[0] == stamp:
            return cached[1], stamp

        # Downscale the cheap 1-channel frame BEFORE any colour-convert/draw.
        src = frame.image
        full_h, full_w = src.shape[:2]
        width = self._cfg.preview_width
        if full_w > width:
            scale = width / full_w
            small = cv2.resize(
                src,
                (width, max(1, int(round(full_h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            scale = 1.0
            small = src
        # Cosmetic preview brightness boost (view-only) on the small image.
        if self._cfg.preview_gain != 1.0:
            small = cv2.convertScaleAbs(small, alpha=self._cfg.preview_gain)
        image = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)

        def box(roi):  # full-frame ROI -> downscaled preview rectangle
            c = roi.clamp(src.shape)
            x, y = int(c.x * scale), int(c.y * scale)
            return x, y, int(c.w * scale), int(c.h * scale)

        if motion is not None and motion.tracks:
            # Multi-object mode: one amber box + small track-id label per track.
            for track in motion.tracks:
                x, y, w, h = box(track.roi)
                cv2.rectangle(image, (x, y), (x + w, y + h), _MOTION_COLOR, 2)
                cv2.putText(
                    image, track.track_id, (x, max(14, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, _MOTION_COLOR, 1,
                )
        elif motion is not None and motion.active and motion.roi is not None:
            x, y, w, h = box(motion.roi)
            cv2.rectangle(image, (x, y), (x + w, y + h), _MOTION_COLOR, 2)
        for decode in decodes:
            x, y, w, h = box(decode.roi)
            cv2.rectangle(image, (x, y), (x + w, y + h), _DECODE_COLOR, 3)
            cv2.putText(
                image, decode.payload, (x, max(20, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, _DECODE_COLOR, 2,
            )
        header = (
            f"{frame.source_id}  f={frame.frame_index}  t={frame.ts:.2f}s"
            + ("  MOTION" if motion is not None and motion.active else "")
        )
        cv2.putText(
            image, header, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, _TEXT_COLOR, 2
        )
        ok, buf = cv2.imencode(
            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self._cfg.preview_quality]
        )
        if not ok:  # pragma: no cover - encoder failure
            return None, stamp
        data: bytes = buf.tobytes()
        with self._lock:
            self._cache = (stamp, data)
        return data, stamp
