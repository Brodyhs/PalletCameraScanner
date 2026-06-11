"""Record a SyntheticSource scenario to a video clip + ground-truth JSONL.

The container is .avi/MJPG, not mp4/H.264: OpenCV's bundled mp4 encoders
are lossy enough to perturb the decodability envelope and H.264 encoder
availability varies by platform, while MJPG (per-frame JPEG) is
pip-only-safe and high-fidelity. Replay itself accepts any .mp4/.avi the
OS can decode (spec §4); this constraint is on *recording* only.

Frames are written as BGR (gray replicated to 3 channels): single-channel
VideoWriter support varies by backend, and the round-trip back to gray at
replay ingest is lossless for an achromatic image.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2

from palletscan.config import AppConfig
from palletscan.sources.factory import synthetic_tail_s
from palletscan.sources.synthetic import SyntheticSource

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecordResult:
    clip_path: Path
    truth_path: Path
    frames: int
    fps: float


def record_synthetic_clip(
    cfg: AppConfig,
    out_path: Path | str,
    truth_path: Path | str | None = None,
) -> RecordResult:
    """Render the configured synthetic scenario to ``out_path`` (.avi/MJPG)
    plus a truth JSONL (default: alongside the clip, ``<clip>.truth.jsonl``).
    """
    out = Path(out_path)
    if out.suffix.lower() != ".avi":
        raise ValueError(
            f"recording writes MJPG into an .avi container, got {out.name!r} "
            "(see module docstring; replay accepts other containers)"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    truth = Path(truth_path) if truth_path is not None else out.with_suffix(
        ".truth.jsonl"
    )

    source = SyntheticSource(cfg.synthetic, tail_s=synthetic_tail_s(cfg))
    writer = cv2.VideoWriter(
        str(out),
        cv2.VideoWriter.fourcc(*"MJPG"),
        cfg.synthetic.fps,
        (cfg.synthetic.width, cfg.synthetic.height),
        isColor=True,
    )
    if not writer.isOpened():
        raise RuntimeError(f"could not open VideoWriter for {out}")
    frames = 0
    try:
        for frame in source.frames():
            writer.write(cv2.cvtColor(frame.image, cv2.COLOR_GRAY2BGR))
            frames += 1
    finally:
        writer.release()
        source.close()
    source.write_truth_jsonl(truth)
    log.info(
        "recorded %d frames @ %.1f fps to %s (truth: %s)",
        frames,
        cfg.synthetic.fps,
        out,
        truth,
    )
    return RecordResult(
        clip_path=out, truth_path=truth, frames=frames, fps=cfg.synthetic.fps
    )
