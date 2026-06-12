"""Source factory: config -> FrameSource (the seam Phase 3 cameras join)."""

from __future__ import annotations

from palletscan.config import AppConfig
from palletscan.sources.base import FrameSource
from palletscan.sources.synthetic import SyntheticSource
from palletscan.sources.video import VideoFileSource


def synthetic_tail_s(cfg: AppConfig) -> float:
    """Trailing idle for a synthetic run: the source must outlast segment
    close + post-roll or the final pass's miss evidence is truncated at
    flush. Shared by the live pipeline and the recording tool so recorded
    clips replay with identical end-of-stream behavior."""
    return cfg.motion.quiet_frames / cfg.synthetic.fps + cfg.buffer.post_s + 0.5


def create_source(
    cfg: AppConfig,
    *,
    epoch: float | None = None,
    epoch_wall: float | None = None,
) -> FrameSource:
    """Build the configured FrameSource.

    ``epoch``/``epoch_wall`` (StationRunner's shared clock anchor, REVIEW
    finding b8) apply to camera sources ONLY: synthetic/video timestamps
    are ``frame_index / fps`` by contract (ASSUMPTIONS #15) and must never
    be re-anchored.
    """
    if cfg.source.type == "synthetic":
        return SyntheticSource(cfg.synthetic, tail_s=synthetic_tail_s(cfg))
    if cfg.source.type == "video":
        return VideoFileSource(cfg.video)
    if cfg.source.type == "camera":
        # Lazy: the live-camera stack only loads when actually configured.
        from palletscan.sources.camera import build_camera_source

        return build_camera_source(cfg, epoch=epoch, epoch_wall=epoch_wall)
    raise ValueError(
        f"unsupported source type {cfg.source.type!r}"
    )  # pragma: no cover - Literal-validated upstream
