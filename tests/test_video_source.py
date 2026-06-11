"""VideoFileSource: gray ingest, native clock, looping, pacing, fps fallback."""

from __future__ import annotations

import itertools
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from palletscan.config import AppConfig, VideoConfig
from palletscan.sources.factory import create_source
from palletscan.sources.synthetic import SyntheticSource
from palletscan.sources.video import VideoFileSource, to_gray


def _write_clip(
    path: Path, n_frames: int = 6, fps: float = 20.0, size: tuple[int, int] = (64, 48)
) -> None:
    """Tiny MJPG clip; each frame's mean brightness encodes its index."""
    w, h = size
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h), isColor=True
    )
    assert writer.isOpened()
    for i in range(n_frames):
        frame = np.full((h, w, 3), (30 + i * 30) % 256, np.uint8)
        writer.write(frame)
    writer.release()


def test_frames_are_grayscale_with_native_clock(tmp_path: Path) -> None:
    clip = tmp_path / "clip.avi"
    _write_clip(clip, n_frames=6, fps=20.0)
    src = VideoFileSource(VideoConfig(path=clip, speed=0))
    frames = list(src.frames())
    src.close()
    assert len(frames) == 6
    for i, f in enumerate(frames):
        assert f.image.ndim == 2 and f.image.dtype == np.uint8
        assert f.frame_index == i
        assert f.ts == pytest.approx(i / 20.0)
    assert src.nominal_fps == pytest.approx(20.0)
    assert src.live is False
    # Frame content survived the codec round trip (index-coded brightness).
    assert float(frames[0].image.mean()) == pytest.approx(30.0, abs=6.0)
    assert float(frames[5].image.mean()) == pytest.approx(180.0, abs=6.0)


def test_to_gray_handles_all_layouts() -> None:
    color = np.zeros((4, 4, 3), np.uint8)
    color[..., 2] = 255  # pure red (BGR) -> luma ~76
    assert to_gray(color).shape == (4, 4)
    assert int(to_gray(color)[0, 0]) == pytest.approx(76, abs=3)
    single = np.full((4, 4, 1), 9, np.uint8)
    assert to_gray(single).shape == (4, 4)
    gray = np.full((4, 4), 7, np.uint8)
    assert to_gray(gray) is gray


def test_ts_monotonic_across_loops(tmp_path: Path) -> None:
    clip = tmp_path / "clip.avi"
    _write_clip(clip, n_frames=5, fps=10.0)
    src = VideoFileSource(VideoConfig(path=clip, speed=0, loop=3))
    frames = list(src.frames())
    src.close()
    assert len(frames) == 15
    assert [f.frame_index for f in frames] == list(range(15))
    ts = [f.ts for f in frames]
    assert ts == sorted(ts) and len(set(ts)) == 15
    assert ts[14] == pytest.approx(14 / 10.0)


def test_loop_forever_until_closed(tmp_path: Path) -> None:
    clip = tmp_path / "clip.avi"
    _write_clip(clip, n_frames=4, fps=10.0)
    src = VideoFileSource(VideoConfig(path=clip, speed=0, loop=0))
    frames = list(itertools.islice(src.frames(), 25))  # > 6 full plays
    src.close()
    assert len(frames) == 25
    assert frames[-1].frame_index == 24


def test_speed_zero_is_unpaced_and_speed_paces(tmp_path: Path) -> None:
    clip = tmp_path / "clip.avi"
    _write_clip(clip, n_frames=20, fps=10.0)  # 2.0 s native duration

    started = time.monotonic()
    src = VideoFileSource(VideoConfig(path=clip, speed=0))
    n = sum(1 for _ in src.frames())
    unpaced_s = time.monotonic() - started
    src.close()
    assert n == 20
    assert unpaced_s < 1.0  # far below native duration

    started = time.monotonic()
    src = VideoFileSource(VideoConfig(path=clip, speed=4.0))  # ~0.5 s target
    sum(1 for _ in src.frames())
    paced_s = time.monotonic() - started
    src.close()
    assert 0.3 <= paced_s < 1.5


def test_fps_metadata_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.avi"
    _write_clip(clip, n_frames=3, fps=20.0)
    monkeypatch.setattr(cv2.VideoCapture, "get", lambda self, prop: 0.0)
    with pytest.raises(ValueError, match="fps_override"):
        VideoFileSource(VideoConfig(path=clip))
    src = VideoFileSource(VideoConfig(path=clip, fps_override=12.0, speed=0))
    frames = list(src.frames())
    src.close()
    assert src.nominal_fps == 12.0
    assert frames[2].ts == pytest.approx(2 / 12.0)


def test_fps_override_wins_over_metadata(tmp_path: Path) -> None:
    clip = tmp_path / "clip.avi"
    _write_clip(clip, n_frames=2, fps=20.0)
    src = VideoFileSource(VideoConfig(path=clip, fps_override=5.0, speed=0))
    assert src.nominal_fps == 5.0
    src.close()


def test_missing_and_corrupt_files_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        VideoFileSource(VideoConfig(path=tmp_path / "nope.avi"))
    with pytest.raises(ValueError, match="video.path is required"):
        VideoFileSource(VideoConfig())
    junk = tmp_path / "junk.avi"
    junk.write_bytes(b"this is not a video file")
    with pytest.raises(ValueError):
        src = VideoFileSource(VideoConfig(path=junk))
        list(src.frames())  # some backends only fail at first read


def test_video_config_rejects_non_finite_values() -> None:
    """NaN compares False to every bound, so it would pass <=/< checks and
    poison every Frame.ts downstream with no error anywhere."""
    nan = float("nan")
    with pytest.raises(ValueError):
        VideoConfig(path="x.avi", fps_override=nan)
    with pytest.raises(ValueError):
        VideoConfig(path="x.avi", speed=nan)
    with pytest.raises(ValueError):
        VideoConfig(path="x.avi", fps_override=float("inf"))
    with pytest.raises(ValueError):
        VideoConfig(path="x.avi", fps_override=0.0)


def test_factory_builds_configured_source(tmp_path: Path) -> None:
    assert isinstance(create_source(AppConfig()), SyntheticSource)
    clip = tmp_path / "clip.avi"
    _write_clip(clip)
    cfg = AppConfig.model_validate(
        {"source": {"type": "video"}, "video": {"path": str(clip)}}
    )
    src = create_source(cfg)
    assert isinstance(src, VideoFileSource)
    src.close()
