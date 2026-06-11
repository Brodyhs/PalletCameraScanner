"""Structured logging setup (minimal JSON lines; rotation arrives in Phase 5)."""

from __future__ import annotations

import json
import logging
import sys
import time


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
