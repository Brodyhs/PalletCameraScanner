"""Config model defaults, YAML loading, validation, CLI overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from palletscan.config import (
    AppConfig,
    ExecutorKind,
    MotionAlgorithm,
    apply_overrides,
    load_config,
)
from palletscan.types import Symbology


def test_defaults_without_file() -> None:
    cfg = load_config(None)
    assert cfg.synthetic.fps == 30.0
    assert cfg.synthetic.px_per_module_range == (3.0, 6.0)
    assert cfg.synthetic.exposure_fraction == pytest.approx(0.03)
    assert cfg.motion.algorithm is MotionAlgorithm.FRAMEDIFF
    assert cfg.decode.executor is ExecutorKind.THREAD
    assert cfg.decode.symbology_priority == [Symbology.QR, Symbology.DATAMATRIX]
    assert cfg.dedup.window_s == 12.0


def test_empty_yaml_yields_defaults(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(p) == AppConfig()


def test_partial_yaml_merges_with_defaults(tmp_path: Path) -> None:
    p = tmp_path / "partial.yaml"
    p.write_text(
        "synthetic:\n  num_passes: 7\n  seed: 99\nmotion:\n  algorithm: mog2\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.synthetic.num_passes == 7
    assert cfg.synthetic.seed == 99
    assert cfg.motion.algorithm is MotionAlgorithm.MOG2
    assert cfg.decode.frame_budget_ms == 50.0  # untouched default


def test_default_yaml_file_is_valid() -> None:
    repo_default = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
    cfg = load_config(repo_default)
    assert cfg == AppConfig(), "config/default.yaml must mirror code defaults"


def test_unknown_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "typo.yaml"
    p.write_text("synthetic:\n  num_pases: 5\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_bad_range_rejected(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("synthetic:\n  speed_mph_range: [10.0, 2.0]\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_non_mapping_root_rejected(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)


def test_apply_overrides() -> None:
    cfg = AppConfig()
    out = apply_overrides(cfg, num_passes=42, seed=7, data_dir="run1")
    assert out.synthetic.num_passes == 42
    assert out.synthetic.seed == 7
    assert out.evidence.dir == Path("run1/evidence")
    assert out.sinks.jsonl.path == Path("run1/events.jsonl")
    assert out.sinks.sqlite.path == Path("run1/palletscan.db")
    # original untouched
    assert cfg.synthetic.num_passes == 20


def test_apply_overrides_noop_returns_equal_config() -> None:
    cfg = AppConfig()
    assert apply_overrides(cfg) == cfg
