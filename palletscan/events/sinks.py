"""Event sinks: console, JSONL file, SQLite.

Sinks are owned and driven exclusively by the EventBus thread, so they need
no internal locking; the SQLite connection is created lazily on first write
(i.e. on the bus thread), satisfying sqlite3's same-thread rule by
construction. The HTTP store-and-forward sink joins in Phase 2 behind this
same interface.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import IO, Any

from palletscan.types import Event, MissEvent, PassEvent

log = logging.getLogger(__name__)


def event_to_dict(event: Event) -> dict[str, Any]:
    """Flatten an event to JSON-serializable primitives, tagged with kind."""
    d = dataclasses.asdict(event)
    d["kind"] = event.kind
    if isinstance(event, PassEvent):
        d["symbology"] = event.symbology.value
        d["best_frame"] = list(event.best_frame)
    return d


class Sink(ABC):
    """One event consumer. Methods are called from the EventBus thread only."""

    @abstractmethod
    def handle(self, event: Event) -> None: ...

    def close(self) -> None:
        """Flush and release resources. Idempotent."""


class ConsoleSink(Sink):
    def handle(self, event: Event) -> None:
        if isinstance(event, PassEvent):
            print(
                f"[PASS] {event.payload} ({event.symbology.value}) "
                f"decodes={event.decode_count} cameras={event.cameras}"
            )
        elif isinstance(event, MissEvent):
            print(
                f"[MISS] {event.candidate_id} frames "
                f"{event.first_frame}-{event.last_frame} "
                f"evidence={event.evidence_dir}"
            )


class JsonlSink(Sink):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    def handle(self, event: Event) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        self._file.write(json.dumps(event_to_dict(event)) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT,
    symbology TEXT,
    candidate_id TEXT,
    source_id TEXT,
    first_seen_ts REAL,
    last_seen_ts REAL,
    decode_count INTEGER,
    evidence_dir TEXT,
    wall_time_iso TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_payload ON events(payload);
PRAGMA user_version = 1;
"""


class SqliteSink(Sink):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        return self._conn

    def handle(self, event: Event) -> None:
        conn = self._connection()
        d = event_to_dict(event)
        row: tuple
        if isinstance(event, PassEvent):
            row = (
                event.event_id,
                "pass",
                event.payload,
                event.symbology.value,
                event.candidate_ids[0] if event.candidate_ids else None,
                next(iter(event.cameras), None),
                event.first_seen_ts,
                event.last_seen_ts,
                event.decode_count,
                None,
                event.wall_time_iso,
                json.dumps(d),
            )
        else:
            assert isinstance(event, MissEvent)
            row = (
                event.event_id,
                "miss",
                None,
                None,
                event.candidate_id,
                event.source_id,
                event.start_ts,
                event.end_ts,
                0,
                event.evidence_dir,
                event.wall_time_iso,
                json.dumps(d),
            )
        conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None
