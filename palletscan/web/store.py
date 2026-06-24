"""ReadStore: the dashboard's SQLite read/annotation layer.

Owns the two web tables (``miss_reviews``, ``manifest`` — D7) in the same
file SqliteSink writes; event rows stay sink-owned. Reviews key on the miss
``event_id``, so they survive evidence pruning. Every public method opens a
fresh connection: sync FastAPI routes run in Starlette's threadpool, and a
connection per call satisfies sqlite3's same-thread rule by construction
(WAL + busy_timeout make the cross-connection writes safe).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from palletscan.types import now_iso

log = logging.getLogger(__name__)

_WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS miss_reviews (
    event_id TEXT PRIMARY KEY,
    reviewed INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    reviewed_utc TEXT
);
CREATE TABLE IF NOT EXISTS manifest (
    payload TEXT PRIMARY KEY
);
"""


class ReadStoreError(RuntimeError):
    """The events DB cannot be opened or prepared (bad path, permissions).

    Raised from construction so the CLI can map it to a clean message +
    exit 2 instead of a raw sqlite3 traceback."""


class ReadStore:
    def __init__(self, db_path: Path, manifest_path: Path | None = None) -> None:
        self._db_path = db_path
        self._manifest_path = manifest_path
        try:
            # Parity with SqliteSink._connection: --dashboard on a fresh
            # config must not depend on the sink having created the
            # directory first.
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                conn.executescript(_WEB_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise ReadStoreError(
                f"cannot open events DB at {db_path}: {exc}"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _detail(row: sqlite3.Row) -> dict[str, Any]:
        # One corrupt/torn detail_json must degrade to a placeholder, not 500 the
        # whole endpoint (matches the missing-table / pruned-evidence tolerance).
        try:
            detail: dict[str, Any] = json.loads(row["detail_json"])
        except (json.JSONDecodeError, TypeError, ValueError):
            kind = row["kind"] if "kind" in row.keys() else None
            log.warning("ReadStore: skipping corrupt detail_json row (kind=%s)", kind)
            return {"kind": kind, "corrupt": True}
        return detail

    def recent_events(
        self, limit: int = 50, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first parsed event rows; tolerant of a not-yet-written DB
        (the sink creates the events table lazily on first event)."""
        query = "SELECT * FROM events"
        params: list[Any] = []
        if kind is not None:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            try:
                rows = conn.execute(query, params).fetchall()
            except sqlite3.OperationalError:  # no events table yet
                return []
        return [self._detail(r) for r in rows]

    def misses(
        self, limit: int = 50, unreviewed_only: bool = False
    ) -> list[dict[str, Any]]:
        """Miss rows joined with their review state, newest first."""
        query = (
            "SELECT events.*, r.reviewed, r.note, r.reviewed_utc "
            "FROM events LEFT JOIN miss_reviews r "
            "ON events.event_id = r.event_id WHERE events.kind = 'miss'"
        )
        if unreviewed_only:
            query += " AND (r.reviewed IS NULL OR r.reviewed = 0)"
        query += " ORDER BY events.rowid DESC LIMIT ?"
        with closing(self._connect()) as conn:
            try:
                rows = conn.execute(query, (limit,)).fetchall()
            except sqlite3.OperationalError:
                return []
        out = []
        for row in rows:
            d = self._detail(row)
            d["reviewed"] = bool(row["reviewed"])
            d["review_note"] = row["note"]
            d["reviewed_utc"] = row["reviewed_utc"]
            out.append(d)
        return out

    def mark_reviewed(
        self, event_id: str, reviewed: bool = True, note: str | None = None
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO miss_reviews (event_id, reviewed, note, reviewed_utc) "
                "VALUES (?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                "reviewed=excluded.reviewed, note=excluded.note, "
                "reviewed_utc=excluded.reviewed_utc",
                (event_id, int(reviewed), note, now_iso()),
            )
            conn.commit()

    def replace_manifest(self, payloads: list[str]) -> int:
        """Replace the uploaded manifest; returns the stored count."""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM manifest")
            conn.executemany(
                "INSERT OR IGNORE INTO manifest (payload) VALUES (?)",
                [(p,) for p in payloads],
            )
            conn.commit()
            (count,) = conn.execute("SELECT COUNT(*) FROM manifest").fetchone()
        return int(count)

    def manifest_payloads(self) -> list[str]:
        """Uploaded manifest, falling back to ``report.manifest_path``."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload FROM manifest ORDER BY rowid"
            ).fetchall()
        if rows:
            return [r["payload"] for r in rows]
        if self._manifest_path is not None and self._manifest_path.is_file():
            from palletscan.reporting.manifest import parse_manifest

            try:
                return parse_manifest(
                    self._manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, ValueError):
                # Strict decode, consistently with the upload path's 400:
                # an undecodable or unreadable fallback file degrades with
                # a warning instead of 500ing every report endpoint.
                log.warning(
                    "report.manifest_path %s is not readable UTF-8 CSV; "
                    "ignoring",
                    self._manifest_path,
                )
        return []

    def pass_and_miss_rows(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """All pass and miss detail rows for the A/B report (stored rows are
        already max-revision thanks to the conditional upsert)."""
        with closing(self._connect()) as conn:
            try:
                rows = conn.execute(
                    "SELECT kind, detail_json FROM events ORDER BY rowid"
                ).fetchall()
            except sqlite3.OperationalError:
                return [], []
        passes = [self._detail(r) for r in rows if r["kind"] == "pass"]
        misses = [self._detail(r) for r in rows if r["kind"] == "miss"]
        return passes, misses
