"""Rotating JSONL file handler + age pruning (Phase 5, D3)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

import pytest

from palletscan.config import LogFileConfig
from palletscan.logging_setup import add_rotating_file_handler, prune_old_logs


@pytest.fixture()
def root_logger():
    """Root logger at INFO; any handler added during the test is removed."""
    root = logging.getLogger()
    before = root.handlers[:]
    level = root.level
    root.setLevel(logging.INFO)
    yield root
    for h in root.handlers[:]:
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.handlers[:] = before
    root.setLevel(level)


def _file_cfg(tmp_path: Path, **overrides) -> LogFileConfig:
    base = {"dir": tmp_path / "logs"}
    base.update(overrides)
    return LogFileConfig(**base)


def test_file_handler_writes_parseable_jsonl(tmp_path: Path, root_logger) -> None:
    cfg = _file_cfg(tmp_path)
    handler = add_rotating_file_handler(cfg)
    assert handler is not None
    path = cfg.dir / "palletscan.jsonl"
    assert Path(handler.baseFilename) == path.resolve()
    log = logging.getLogger("rotation-test")
    log.info("hello %s", "world")
    try:
        raise ValueError("boom")
    except ValueError:
        log.error("with traceback", exc_info=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["msg"] == "hello world"
    assert first["level"] == "INFO"
    assert first["logger"] == "rotation-test"
    assert "ts" in first
    assert "ValueError: boom" in second["exc"]


def test_rotation_respects_total_size_cap(tmp_path: Path, root_logger) -> None:
    # ~2 KB per file, 3 backups -> total cap 8 KB; write ~40 KB of records.
    cfg = _file_cfg(tmp_path, max_mb=2048 / (1024 * 1024), backups=3)
    handler = add_rotating_file_handler(cfg)
    assert handler is not None
    path = cfg.dir / "palletscan.jsonl"
    log = logging.getLogger("rotation-cap-test")
    for i in range(300):
        log.info("filler line %04d %s", i, "x" * 80)
    files = sorted(p for p in cfg.dir.iterdir() if p.name.startswith(path.name))
    assert len(files) == cfg.backups + 1, "rollover must keep exactly backups+1"
    cap_bytes = int(cfg.max_mb * 1024 * 1024) * (cfg.backups + 1)
    assert sum(p.stat().st_size for p in files) <= cap_bytes


def test_add_rotating_file_handler_is_idempotent(
    tmp_path: Path, root_logger
) -> None:
    cfg = _file_cfg(tmp_path)
    first = add_rotating_file_handler(cfg)
    assert add_rotating_file_handler(cfg) is first
    rotating = [
        h
        for h in root_logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    # A different filename (the supervisor's own file) is a separate handler.
    add_rotating_file_handler(cfg, filename="supervisor.jsonl")
    rotating = [
        h
        for h in root_logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 2


def test_disabled_file_logging_adds_nothing(tmp_path: Path, root_logger) -> None:
    handlers_before = len(root_logger.handlers)
    assert add_rotating_file_handler(_file_cfg(tmp_path, enabled=False)) is None
    assert len(root_logger.handlers) == handlers_before


def test_prune_old_logs_spares_young_and_restarts(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old_stamp = time.time() - 30 * 86400
    for name in ("palletscan.jsonl.5", "restarts.jsonl"):
        p = logs / name
        p.write_text("{}\n", encoding="utf-8")
        os.utime(p, (old_stamp, old_stamp))
    young = logs / "palletscan.jsonl"
    young.write_text("{}\n", encoding="utf-8")
    deleted = prune_old_logs(logs, max_age_days=14.0)
    assert deleted == 1
    assert not (logs / "palletscan.jsonl.5").exists()
    assert (logs / "restarts.jsonl").exists(), "the audit trail is never pruned"
    assert young.exists()


def test_prune_missing_dir_is_tolerated(tmp_path: Path) -> None:
    assert prune_old_logs(tmp_path / "nonexistent", max_age_days=14.0) == 0
