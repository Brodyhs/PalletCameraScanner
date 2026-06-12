"""Structured logging: JSON lines to stderr always, plus a size-rotating,
age-pruned JSONL file for the lock-holding writer commands (run/synth/replay
— see ``LogFileConfig``; lock scope == file-logging scope keeps Windows
rotation single-writer)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palletscan.config import LogFileConfig

#: The ops audit trail (one JSONL line per supervised child exit, appended
#: by the supervisor); never age-pruned.
RESTARTS_LOG_NAME = "restarts.jsonl"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg (+ exc_info)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(record.created)
            )
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        stats = getattr(record, "stats", None)
        if stats is not None:
            entry["stats"] = stats
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger once; idempotent."""
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def add_rotating_file_handler(
    cfg: "LogFileConfig", filename: str = "palletscan.jsonl"
) -> logging.handlers.RotatingFileHandler | None:
    """Attach a size-rotating JSONL file handler to the root logger.

    Idempotent per target path (returns the existing handler). With
    ``delay=True`` the file opens on first emit, after the caller's
    :func:`prune_old_logs` sweep. Callers must hold the instance lock
    first — ``doRollover`` renames, which fails on Windows while another
    process holds the file open.

    Returns the handler, or ``None`` when file logging is disabled.
    """
    if not cfg.enabled:
        return None
    path = Path(cfg.dir) / filename
    # FileHandler stores os.path.abspath(); compare apples to apples.
    target = os.path.abspath(path)
    root = logging.getLogger()
    for h in root.handlers:
        if (
            isinstance(h, logging.handlers.RotatingFileHandler)
            and h.baseFilename == target
        ):
            return h
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=int(cfg.max_mb * 1024 * 1024),
        backupCount=cfg.backups,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    return handler


def prune_old_logs(log_dir: Path | str, max_age_days: float) -> int:
    """Delete files under ``log_dir`` older than ``max_age_days`` (mtime),
    always sparing ``restarts.jsonl`` (the ops audit trail).

    OSError-tolerant in the EvidenceWriter.prune style: an entry vanishing
    mid-sweep or held open by another process is skipped, never raised —
    pruning is housekeeping, not correctness. Returns the deletion count.
    """
    cutoff = time.time() - max_age_days * 86400.0
    deleted = 0
    try:
        entries = list(Path(log_dir).iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.name == RESTARTS_LOG_NAME:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted
