"""Sinks and EventBus: serialization, SQLite schema, error isolation."""

from __future__ import annotations

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
    assert version == 1
    detail = json.loads(
        conn.execute(
            "SELECT detail_json FROM events WHERE kind='pass'"
        ).fetchone()[0]
    )
    assert detail["cameras"] == {"cam0": 3}
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
