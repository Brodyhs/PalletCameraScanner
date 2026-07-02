"""Config model defaults, YAML loading, validation, CLI overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from palletscan.config import (
    AppConfig,
    DecodeEngineKind,
    ExecutorKind,
    LogFileConfig,
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


def test_station_yaml_matches_defaults_except_documented_deviations() -> None:
    repo_station = Path(__file__).resolve().parents[1] / "config" / "station.yaml"
    cfg = load_config(repo_station)
    # The station.yaml header documents EXACTLY these deviations from code
    # defaults; pin them so the file and its header stay honest.
    assert cfg.watchdog.max_outage_s == 120.0
    assert cfg.decode.engine is DecodeEngineKind.ZXING
    assert len(cfg.cameras) == 2  # the two See3CAMs, station-specific
    got = cfg.model_dump()
    expected = AppConfig().model_dump()
    expected["watchdog"]["max_outage_s"] = got["watchdog"]["max_outage_s"]
    expected["decode"]["engine"] = got["decode"]["engine"]
    expected["cameras"] = got["cameras"]
    assert got == expected, (
        "undocumented deviation from AppConfig() defaults — update the "
        "config/station.yaml header comment AND this test"
    )


@pytest.mark.parametrize("bad", [0, -1])
def test_frame_queue_size_zero_or_negative_rejected(bad: int) -> None:
    # queue.Queue treats maxsize <= 0 as INFINITE, which would silently defeat
    # DroppingQueue's bounded drop-oldest contract — reject at config load.
    with pytest.raises(ValidationError):
        AppConfig(frame_queue_size=bad)


def test_frame_queue_size_default_and_minimum() -> None:
    assert AppConfig().frame_queue_size == 64
    assert AppConfig(frame_queue_size=1).frame_queue_size == 1


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


def test_apply_overrides_rebases_log_dir_and_lock_path() -> None:
    out = apply_overrides(AppConfig(), data_dir="run1")
    assert out.logging.file.dir == Path("run1/logs")
    assert out.lock.path == Path("run1/palletscan.lock")


def test_log_file_config_validators() -> None:
    with pytest.raises(ValidationError):
        LogFileConfig(max_mb=0)
    with pytest.raises(ValidationError):
        LogFileConfig(backups=0)
    with pytest.raises(ValidationError):
        LogFileConfig(max_age_days=0)
    assert LogFileConfig().enabled is True
