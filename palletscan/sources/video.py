"""VideoFileSource: replay recorded clips through the FrameSource seam.

``ts = frame_index / fps`` — the file's *native* clock regardless of
playback speed, so dedup windows, buffer eviction and miss deadlines are
bit-identical at any acceleration (ASSUMPTIONS #15). ``speed`` shapes only
wall-clock delivery: 1.0 paces as-if-live, >1 accelerates, 0 is unpaced.

``live`` is always False: every frame of a file is available, so
backpressure must block, never drop — "as-if-live" means paced, not lossy.
Frames are normalized to single-channel grayscale once at ingest (spec §2).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

from palletscan.config import VideoConfig
from palletscan.sources.base import FrameSource
from palletscan.types import Frame

log = logging.getLogger(__name__)


def to_gray(img: np.ndarray) -> np.ndarray:
    """Normalize a decoded frame to 2-D grayscale (BGR or single-channel in)."""
    if img.ndim == 2:
        return img
    if img.shape[2] == 1:
        return img[:, :, 0]
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


class VideoFileSource(FrameSource):
    """Replays an .mp4/.avi (anything the OS can decode) as a frame stream."""

    def __init__(self, cfg: VideoConfig, source_id: str = "video0") -> None:
        self._cfg = cfg
        self._source_id = source_id
        self._path = Path(cfg.path)
        if str(cfg.path) in ("", "."):
            raise ValueError("video.path is required for source.type=video")
        if not self._path.is_file():
            raise FileNotFoundError(f"video file not found: {self._path}")
        self._cap = self._open()
        meta_fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if cfg.fps_override is not None:
            self._fps = cfg.fps_override
            if meta_fps > 0 and not math.isclose(meta_fps, cfg.fps_override):
                log.info(
                    "fps_override %.3f replaces metadata fps %.3f for %s",
                    cfg.fps_override,
                    meta_fps,
                    self._path,
                )
        elif meta_fps > 0 and math.isfinite(meta_fps):
            self._fps = meta_fps
        else:
            raise ValueError(
                f"{self._path} reports no usable fps metadata "
                f"({meta_fps!r}); set video.fps_override"
            )
        self._closed = False

    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(str(self._path))
        if not cap.isOpened():
            raise ValueError(
                f"could not open video file {self._path} "
                "(unsupported codec or corrupt file)"
            )
        return cap

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def nominal_fps(self) -> float:
        return self._fps

    def frames(self) -> Iterator[Frame]:
        cfg = self._cfg
        idx = 0
        plays = 0
        play_start_idx = 0
        anchor = time.monotonic()
        while not self._closed:
            ok, img = self._cap.read()
            if not ok:
                if idx == play_start_idx:
                    raise ValueError(f"no frames decoded from {self._path}")
                plays += 1
                if cfg.loop and plays >= cfg.loop:
                    break
                # Reopen rather than seek: not every codec rewinds cleanly.
                # frame_index keeps incrementing so ts stays monotonic.
                self._cap.release()
                self._cap = self._open()
                play_start_idx = idx
                continue
            if cfg.speed > 0:
                # Absolute schedule from the anchor (not sleep-per-frame),
                # so pacing self-corrects after any downstream stall.
                delay = anchor + idx / (self._fps * cfg.speed) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            yield Frame(
                image=to_gray(img),
                ts=idx / self._fps,
                frame_index=idx,
                source_id=self._source_id,
            )
            idx += 1

    def close(self) -> None:
        self._closed = True
        self._cap.release()
