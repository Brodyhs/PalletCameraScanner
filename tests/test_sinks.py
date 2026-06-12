"""Sinks and EventBus: serialization, SQLite schema, error isolation."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

from palletscan.events.bus import EventBus
from palletscan.events.sinks import ConsoleSink, JsonlSink, Sink, SqliteSink
from palletscan.types import Event, MissEvent, PassEvent, Symbology


def _pass(payload: str = "PLT-000001") -> PassEvent:
    return PassEvent(
        payload=payload,
        symbology=Symbology.QR,
        first_seen_ts=1.0,
        last_seen_ts=2.0,
        decode_count=3,
        cameras={"cam0": 3},
        best_frame=("cam0", 42),
        candidate_ids=["cam0-000001"],
        event_id=f"ev-{payload}",
        wall_time_iso="2026-06-10T00:00:00+00:00",
        first_decode_ts=1.5,
        camera_detail={
            "cam0": {
                "first_seen_ts": 1.0,
                "first_decode_ts": 1.5,
                "last_seen_ts": 2.0,
                "decode_count": 3,
            }
        },
    )


def _miss() -> MissEvent:
    return MissEvent(
        candidate_id="cam0-000002",
        source_id="cam0",
        start_ts=5.0,
        end_ts=6.0,
        first_frame=150,
        last_frame=180,
        evidence_dir="/tmp/ev/x",
        evidence_frame_count=10,
        event_id="ev-miss-1",
        wall_time_iso="2026-06-10T00:00:01+00:00",
    )


def test_jsonl_sink_writes_valid_json_lines(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "events.jsonl")
    sink.handle(_pass())
    sink.handle(_miss())
    sink.close()
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    a, b = (json.loads(line) for line in lines)
    assert a["kind"] == "pass"
    assert a["payload"] == "PLT-000001"
    assert a["symbology"] == "qr"
    assert a["decode_count"] == 3
    assert b["kind"] == "miss"
    assert b["evidence_dir"] == "/tmp/ev/x"


def test_sqlite_sink_rows_queryable(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    sink = SqliteSink(db)
    sink.handle(_pass())
    sink.handle(_miss())
    sink.close()
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT kind, payload, candidate_id, evidence_dir FROM events ORDER BY kind"
    ).fetchall()
    assert ("miss", None, "cam0-000002", "/tmp/ev/x") in rows
    assert ("pass", "PLT-000001", "cam0-000001", None) in rows
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 2
    detail = json.loads(
        conn.execute(
            "SELECT detail_json FROM events WHERE kind='pass'"
        ).fetchone()[0]
    )
    assert detail["cameras"] == {"cam0": 3}
    # Phase 4 additive fields ride through detail_json untouched.
    assert detail["first_decode_ts"] == 1.5
    assert detail["camera_detail"]["cam0"]["decode_count"] == 3
    assert detail["revision"] == 0
    conn.close()


_V1_SCHEMA = """
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


def test_sqlite_v1_to_v2_migration_preserves_rows(tmp_path: Path) -> None:
    """An existing Phase 1-3 DB gains the revision column without data loss."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ev-old", "pass", "PLT-OLD", "qr", "cam0-000009", "cam0",
         1.0, 2.0, 4, None, "2026-06-01T00:00:00+00:00", "{}"),
    )
    conn.commit()
    conn.close()

    sink = SqliteSink(db)
    sink.handle(_pass())  # triggers connect + migration
    sink.close()

    conn = sqlite3.connect(db)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 2
    old = conn.execute(
        "SELECT payload, decode_count, revision FROM events WHERE event_id='ev-old'"
    ).fetchone()
    assert old == ("PLT-OLD", 4, 0)
    new = conn.execute(
        "SELECT payload, revision FROM events WHERE event_id='ev-PLT-000001'"
    ).fetchone()
    assert new == ("PLT-000001", 0)
    conn.close()


def test_sqlite_stale_revision_cannot_regress_row(tmp_path: Path) -> None:
    """The revision-guarded upsert: a late v0 after v1 is a no-op (D1)."""
    db = tmp_path / "rev.db"
    sink = SqliteSink(db)
    base = _pass()
    merged = dataclasses.replace(
        base,
        decode_count=6,
        cameras={"cam0": 3, "cam1": 3},
        revision=1,
    )
    sink.handle(merged)
    sink.handle(base)  # stale pre-merge version arrives late
    sink.close()

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT decode_count, revision, detail_json FROM events "
        "WHERE event_id=?", (base.event_id,)
    ).fetchone()
    assert row[0] == 6
    assert row[1] == 1
    assert json.loads(row[2])["cameras"] == {"cam0": 3, "cam1": 3}
    conn.close()


def test_sqlite_future_schema_refused_on_every_write(tmp_path: Path) -> None:
    """A failed migration must not be bypassed by connection caching: every
    handle() against a newer-than-this-build DB refuses, and the DB stays
    untouched (adversarial-review finding)."""
    import pytest

    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.executescript(_V1_SCHEMA + "PRAGMA user_version = 9;")
    conn.commit()
    conn.close()

    sink = SqliteSink(db)
    for _ in range(2):  # second attempt must not silently write
        with pytest.raises(RuntimeError, match="newer than this build"):
            sink.handle(_pass())
    sink.close()
    conn = sqlite3.connect(db)
    (count,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    conn.close()
    assert count == 0
    assert version == 9


def test_sqlite_equal_revision_replaces(tmp_path: Path) -> None:
    """Same-revision re-handle (at-least-once redelivery) still lands."""
    db = tmp_path / "eq.db"
    sink = SqliteSink(db)
    sink.handle(_pass())
    sink.handle(dataclasses.replace(_pass(), decode_count=9))
    sink.close()
    conn = sqlite3.connect(db)
    (count,) = conn.execute(
        "SELECT decode_count FROM events WHERE event_id='ev-PLT-000001'"
    ).fetchone()
    assert count == 9
    conn.close()


def test_console_sink_smoke(capsys) -> None:
    ConsoleSink().handle(_pass())
    ConsoleSink().handle(_miss())
    out = capsys.readouterr().out
    assert "[PASS] PLT-000001" in out
    assert "[MISS] cam0-000002" in out


class _BoomSink(Sink):
    def handle(self, event: Event) -> None:
        raise RuntimeError("boom")


class _ListSink(Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.closed = False

    def handle(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


def test_bus_isolates_failing_sink_and_drains_on_shutdown() -> None:
    good = _ListSink()
    bus = EventBus([_BoomSink(), good])
    bus.start()
    for i in range(10):
        bus.publish(_pass(f"PLT-{i:06d}"))
    bus.shutdown()
    assert len(good.events) == 10
    assert bus.sink_errors == 10
    assert bus.events_handled == 10
    assert good.closed


# -- REVIEW_SYSTEM_0c30c77 finding 11 ------------------------------------------


def test_wedged_sink_makes_shutdown_report_undrained() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 11 (repro: one hung sink write let
    shutdown() time out silently; everything still queued died with the
    daemon thread at exit 0). The caller must be able to fail the run."""
    import threading

    release = threading.Event()

    class _WedgedSink(Sink):
        def handle(self, event: Event) -> None:
            release.wait(timeout=30.0)

    bus = EventBus([_WedgedSink()], join_timeout_s=0.3)
    bus.start()
    for _ in range(3):
        bus.publish(_pass("PLT-WEDGE"))
    try:
        assert bus.shutdown() is False
        assert bus.events_lost >= 1, "the lost tail must be counted"
    finally:
        release.set()


def test_clean_shutdown_reports_drained() -> None:
    """Finding 11 control: a healthy sink set drains and reports True."""
    seen: list[Event] = []

    class _ListSink(Sink):
        def handle(self, event: Event) -> None:
            seen.append(event)

    bus = EventBus([_ListSink()], join_timeout_s=5.0)
    bus.start()
    bus.publish(_pass("PLT-OK"))
    assert bus.shutdown() is True
    assert bus.events_lost == 0
    assert len(seen) == 1


def test_publish_after_shutdown_is_counted_never_silent() -> None:
    """Finding 11, station variant: the business SENTINEL could overtake a
    straggling per-camera bus still submitting through the deduper —
    events behind the SENTINEL were never handled while counters kept
    incrementing. The overtake is now counted and logged."""
    import threading

    release = threading.Event()

    class _SlowSink(Sink):
        def handle(self, event: Event) -> None:
            release.wait(timeout=30.0)

    bus = EventBus([_SlowSink()], join_timeout_s=0.3)
    bus.start()
    bus.publish(_pass("PLT-1"))  # parks the bus thread in the sink
    shutdown_result: list[bool] = []
    shutdown_thread = threading.Thread(
        target=lambda: shutdown_result.append(bus.shutdown())
    )
    shutdown_thread.start()
    while not bus._shutdown_started:
        pass
    bus.publish(_pass("PLT-LATE"))  # behind the SENTINEL: unreachable
    release.set()
    shutdown_thread.join(timeout=5.0)
    assert bus.published_after_shutdown == 1
