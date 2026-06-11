"""SyntheticSource: truth alignment, determinism, ratio invariance."""

from __future__ import annotations

import numpy as np
import pytest

from palletscan.config import SyntheticConfig
from palletscan.sources.synthetic import SyntheticSource
from palletscan.types import Frame


def _cfg(**overrides) -> SyntheticConfig:
    base = dict(
        width=640,
        height=360,
        fps=30.0,
        seed=77,
        num_passes=4,
        speed_mph_range=(3.0, 8.0),
        angle_deg_range=(0.0, 20.0),
        idle_s_range=(0.3, 0.5),
    )
    base.update(overrides)
    return SyntheticConfig(**base)


def _run(src: SyntheticSource) -> list[Frame]:
    return list(src.frames())


def test_truth_records_align_with_emitted_frames() -> None:
    src = SyntheticSource(_cfg())
    frames = _run(src)
    assert len(src.truth) == 4
    last_end = -1
    for rec in src.truth:
        assert rec.first_frame > last_end, "passes must not overlap"
        assert rec.last_frame > rec.first_frame
        assert rec.last_frame < len(frames)
        last_end = rec.last_frame
    # ts is the simulated source clock
    assert frames[10].ts == pytest.approx(10 / 30.0)
    assert all(f.image.dtype == np.uint8 and f.image.ndim == 2 for f in frames)


def test_truth_params_include_dimensionless_ratios() -> None:
    src = SyntheticSource(_cfg())
    _run(src)
    for rec in src.truth:
        assert 3.0 <= rec.params["px_per_module"] <= 6.0 * 1.05
        assert rec.params["blur_modules"] >= 0.0
        assert rec.params["blur_px"] == pytest.approx(
            rec.params["blur_modules"] * rec.params["px_per_module"]
        )


def test_blur_at_default_exposure_is_at_most_one_module() -> None:
    """The ~1 ms operating point keeps blur <= ~1 module even at 10 mph."""
    cfg = _cfg(speed_mph_range=(10.0, 10.0), num_passes=2)
    src = SyntheticSource(cfg)
    _run(src)
    for rec in src.truth:
        assert rec.params["blur_modules"] <= 1.0


def test_idle_frames_are_noise_only_and_pass_frames_move() -> None:
    cfg = _cfg(noise_sigma_range=(0.0, 0.0), num_passes=2)
    src = SyntheticSource(cfg)
    frames = _run(src)
    first = src.truth[0]
    # With zero noise, idle frames are bit-identical (static scene).
    assert first.first_frame >= 2
    idle_a, idle_b = frames[first.first_frame - 2], frames[first.first_frame - 1]
    assert np.array_equal(idle_a.image, idle_b.image)
    # Mid-pass consecutive frames differ (the pallet moved).
    mid = (first.first_frame + first.last_frame) // 2
    assert not np.array_equal(frames[mid].image, frames[mid + 1].image)


def test_deterministic_per_seed() -> None:
    a = SyntheticSource(_cfg())
    b = SyntheticSource(_cfg())
    c = SyntheticSource(_cfg(seed=78))
    fa, fb, fc = _run(a), _run(b), _run(c)
    assert len(fa) == len(fb)
    sample = range(0, len(fa), max(1, len(fa) // 7))
    for i in sample:
        assert np.array_equal(fa[i].image, fb[i].image)
    assert a.truth == b.truth
    assert any(
        not np.array_equal(fa[i].image, fc[i].image)
        for i in sample
        if i < len(fc)
    ) or a.truth != c.truth


def test_ratios_invariant_across_frame_sizes() -> None:
    """px/module and blur-in-modules must not change with resolution."""
    big = SyntheticSource(_cfg(width=1280, height=720))
    small = SyntheticSource(_cfg(width=640, height=360))
    _run(big), _run(small)
    for rb, rs in zip(big.truth, small.truth):
        assert rb.params["px_per_module"] == pytest.approx(
            rs.params["px_per_module"]
        )
        assert rb.params["blur_modules"] == pytest.approx(
            rs.params["blur_modules"]
        )
        assert rb.payload == rs.payload


def test_truth_jsonl_roundtrip(tmp_path) -> None:
    import json

    src = SyntheticSource(_cfg(num_passes=2))
    _run(src)
    out = tmp_path / "truth.jsonl"
    src.write_truth_jsonl(out)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["payload"] == src.truth[0].payload
    assert "px_per_module" in lines[0] and "blur_modules" in lines[0]
