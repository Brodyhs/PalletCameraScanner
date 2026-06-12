"""ReadStore: web-owned tables, review persistence, manifest round-trip."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from palletscan.events.sinks import SqliteSink
from palletscan.types import MissEvent, PassEvent, Symbology
from palletscan.web.store import ReadStore


def _pass(payload: str, event_id: str) -> PassEvent:
    return PassEvent(
        payload=payload,
        symbology=Symbology.QR,
        first_seen_ts=1.0,
        last_seen_ts=2.0,
        decode_count=3,
        cameras={"camA": 3},
        best_frame=("camA", 42),
        candidate_ids=["camA-000001"],
        event_id=event_id,
        wall_time_iso="2026-06-11T00:00:00+00:00",
        first_decode_ts=1.5,
        camera_detail={
            "camA": {
                "first_seen_ts": 1.0,
                "first_decode_ts": 1.5,
                "last_seen_ts": 2.0,
                "decode_count": 3,
            }
        },
    )


def _miss(event_id: str, evidence_dir: str = "/tmp/ev/x") -> MissEvent:
    return MissEvent(
        candidate_id="camA-000002",
        source_id="camA",
        start_ts=5.0,
        end_ts=6.0,
        first_frame=150,
        last_frame=180,
        evidence_dir=evidence_dir,
        evidence_frame_count=10,
        event_id=event_id,
        wall_time_iso="2026-06-11T00:00:01+00:00",
    )


def _seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "events.db"
    sink = SqliteSink(db)
    sink.handle(_pass("PLT-000001", "ev-p1"))
    sink.handle(_miss("ev-m1"))
    sink.handle(_pass("PLT-000002", "ev-p2"))
    sink.close()
    return db


def test_recent_events_newest_first_with_kind_filter(tmp_path: Path) -> None:
    store = ReadStore(_seeded_db(tmp_path))
    events = store.recent_events()
    assert [e["event_id"] for e in events] == ["ev-p2", "ev-m1", "ev-p1"]
    passes = store.recent_events(kind="pass")
    assert [e["event_id"] for e in passes] == ["ev-p2", "ev-p1"]
    assert passes[0]["camera_detail"]["camA"]["decode_count"] == 3
    assert store.recent_events(limit=1) == [events[0]]


def test_recent_events_empty_before_sink_writes(tmp_path: Path) -> None:
    # The sink creates the events table lazily; a dashboard attached to a
    # fresh run must serve [] rather than 500.
    store = ReadStore(tmp_path / "fresh.db")
    assert store.recent_events() == []
    assert store.misses() == []
    assert store.pass_and_miss_rows() == ([], [])


def test_review_round_trip_persists_across_instances(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    store = ReadStore(db)
    misses = store.misses()
    assert len(misses) == 1
    assert misses[0]["reviewed"] is False
    store.mark_reviewed("ev-m1", note="forklift blocked the code")
    fresh = ReadStore(db)  # reviews survive process restarts
    reviewed = fresh.misses()
    assert reviewed[0]["reviewed"] is True
    assert reviewed[0]["review_note"] == "forklift blocked the code"
    assert reviewed[0]["reviewed_utc"] is not None
    assert fresh.misses(unreviewed_only=True) == []
    store.mark_reviewed("ev-m1", reviewed=False)
    assert len(fresh.misses(unreviewed_only=True)) == 1


def test_manifest_replace_and_read_back(tmp_path: Path) -> None:
    store = ReadStore(_seeded_db(tmp_path))
    assert store.manifest_payloads() == []
    count = store.replace_manifest(["PLT-1", "PLT-2", "PLT-2"])
    assert count == 2  # dupes collapse at the PK
    assert store.manifest_payloads() == ["PLT-1", "PLT-2"]
    assert store.replace_manifest(["PLT-9"]) == 1
    assert store.manifest_payloads() == ["PLT-9"]


def test_manifest_config_path_fallback(tmp_path: Path) -> None:
    csv_file = tmp_path / "expected.csv"
    csv_file.write_text("pallet_id\nPLT-7\nPLT-8\n", encoding="utf-8")
    store = ReadStore(_seeded_db(tmp_path), manifest_path=csv_file)
    # Empty table -> config-pointed CSV wins.
    assert store.manifest_payloads() == ["PLT-7", "PLT-8"]
    # An uploaded manifest overrides the file fallback.
    store.replace_manifest(["PLT-1"])
    assert store.manifest_payloads() == ["PLT-1"]


def test_pass_and_miss_rows_split(tmp_path: Path) -> None:
    store = ReadStore(_seeded_db(tmp_path))
    passes, misses = store.pass_and_miss_rows()
    assert {p["payload"] for p in passes} == {"PLT-000001", "PLT-000002"}
    assert len(misses) == 1
    assert misses[0]["evidence_dir"] == "/tmp/ev/x"


def test_concurrent_writer_busy_timeout_smoke(tmp_path: Path) -> None:
    """A sink connection holding the WAL write lock must not make the
    store's write raise SQLITE_BUSY instantly (busy_timeout, D6)."""
    db = _seeded_db(tmp_path)
    store = ReadStore(db)
    # check_same_thread=False: the release Timer fires on its own thread.
    blocker = sqlite3.connect(db, check_same_thread=False)
    try:
        blocker.execute("BEGIN IMMEDIATE")  # hold the write lock briefly
        import threading

        timer = threading.Timer(0.3, blocker.commit)
        timer.start()
        store.mark_reviewed("ev-m1")  # must wait ~0.3 s, then succeed
        timer.join()
    finally:
        blocker.close()
    assert ReadStore(db).misses()[0]["reviewed"] is True
