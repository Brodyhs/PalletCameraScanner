"""Operator sessions: dashboard-driven start/close with acknowledge-to-close.

A session is a reporting window over the always-running pipeline (never a
gate on it): counts are business-level (cross-camera deduped) pass/miss
totals between the start and close stamps. Closing with a count mismatch
requires an operator acknowledgement note (the 409 drives the UI prompt).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from palletscan.config import MetricsConfig, WebConfig
from palletscan.events.sinks import SqliteSink
from palletscan.metrics import MetricsRegistry
from palletscan.types import MissEvent, PassEvent, Symbology, now_iso
from palletscan.web.app import DashboardContext, create_app
from palletscan.web.store import ReadStore

_OLD_ISO = "2020-01-01T00:00:00+00:00"  # long before any session window


def _pass(payload: str, event_id: str, iso: str) -> PassEvent:
    return PassEvent(
        payload=payload,
        symbology=Symbology.QR,
        first_seen_ts=1.0,
        last_seen_ts=2.0,
        decode_count=3,
        cameras={"camA": 3},
        best_frame=("camA", 42),
        candidate_ids=[f"camA-{event_id}"],
        event_id=event_id,
        wall_time_iso=iso,
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


def _miss(event_id: str, iso: str) -> MissEvent:
    return MissEvent(
        candidate_id=f"camA-{event_id}",
        source_id="camA",
        start_ts=5.0,
        end_ts=6.0,
        first_frame=150,
        last_frame=180,
        evidence_dir="data/evidence/2026-07-02/gone",
        evidence_frame_count=0,
        event_id=event_id,
        wall_time_iso=iso,
    )


def _write(db: Path, *events) -> None:
    sink = SqliteSink(db)
    for event in events:
        sink.handle(event)
    sink.close()


@pytest.fixture()
def web(tmp_path: Path):
    db = tmp_path / "events.db"
    metrics = MetricsRegistry(MetricsConfig())
    ctx = DashboardContext(
        snapshots={"camA": metrics.snapshot},
        previews={},
        business=None,
        store=ReadStore(db),
        evidence_root=tmp_path / "evidence",
        web=WebConfig(),
    )
    return TestClient(create_app(ctx)), db


def test_session_lifecycle_counts_and_matching_close(web) -> None:
    client, db = web
    assert client.get("/api/session").json() == {"session": None}

    resp = client.post("/api/session/start", json={"expected_count": 2})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "open"
    assert body["counts"] == {"decoded": 0, "missed": 0, "shortfall": 2}

    # One open session at a time.
    dup = client.post("/api/session/start", json={"expected_count": 5})
    assert dup.status_code == 409
    assert "already open" in dup.json()["detail"]

    # A stale (pre-session) pass must not count; in-window pass + miss do.
    _write(
        db,
        _pass("PLT-OLD", "ev-old", _OLD_ISO),
        _pass("PLT-000001", "ev-p1", now_iso()),
        _miss("ev-m1", now_iso()),
    )
    live = client.get("/api/session").json()["session"]
    assert live["counts"] == {"decoded": 1, "missed": 1, "shortfall": 0}

    # Counts add up: closes without an acknowledgement.
    closed = client.post("/api/session/close", json={})
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "closed"
    assert body["ack_note"] is None
    assert body["counts"] == {"decoded": 1, "missed": 1, "shortfall": 0}
    assert body["closed_utc"] is not None
    assert body["cameras"]["camA"]["misses"] == 1

    assert client.get("/api/session").json() == {"session": None}
    history = client.get("/api/session/history").json()
    assert [s["status"] for s in history] == ["closed"]
    # The persisted summary survives re-reads (not recomputed from live data).
    assert history[0]["counts"] == {"decoded": 1, "missed": 1, "shortfall": 0}

    csv_resp = client.get(f"/report/session/{body['id']}.csv")
    assert csv_resp.status_code == 200
    assert "shortfall,0" in csv_resp.text.replace("\r", "")
    assert "attachment" in csv_resp.headers["content-disposition"]


def test_close_mismatch_requires_acknowledgement(web) -> None:
    client, db = web
    client.post("/api/session/start", json={"expected_count": 3})
    _write(db, _pass("PLT-000001", "ev-p1", now_iso()))

    refused = client.post("/api/session/close", json={})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["requires_ack"] is True
    assert (detail["expected"], detail["decoded"], detail["missed"]) == (3, 1, 0)
    # Still open: the refusal must not half-close it.
    assert client.get("/api/session").json()["session"]["status"] == "open"

    # A whitespace-only note is not an acknowledgement.
    refused2 = client.post("/api/session/close", json={"ack_note": "   "})
    assert refused2.status_code == 409

    closed = client.post(
        "/api/session/close", json={"ack_note": "only one object presented"}
    )
    assert closed.status_code == 200
    body = closed.json()
    assert body["ack_note"] == "only one object presented"
    assert body["counts"]["shortfall"] == 2


def test_close_without_open_session_is_409(web) -> None:
    client, _ = web
    assert client.post("/api/session/close", json={}).status_code == 409


def test_start_rejects_non_positive_expected(web) -> None:
    client, _ = web
    assert (
        client.post("/api/session/start", json={"expected_count": 0}).status_code
        == 422
    )


def test_close_summary_windows_the_manifest_reconciliation(web) -> None:
    client, db = web
    # Manifest expects two payloads; only PLT-A is scanned INSIDE the session
    # (PLT-B was scanned long before it), so the session-level manifest view
    # must show B missing even though a whole-DB reconciliation would not.
    assert (
        client.post(
            "/api/manifest",
            content="payload\nPLT-A\nPLT-B\n",
            headers={"Content-Type": "text/csv"},
        ).status_code
        == 200
    )
    _write(db, _pass("PLT-B", "ev-old", _OLD_ISO))
    client.post("/api/session/start", json={"expected_count": 1})
    _write(db, _pass("PLT-A", "ev-p1", now_iso()))

    closed = client.post("/api/session/close", json={})
    assert closed.status_code == 200
    manifest = closed.json()["manifest"]
    assert manifest["matched"] == ["PLT-A"]
    assert manifest["missing"] == ["PLT-B"]


def test_session_csv_unknown_id_is_404(web) -> None:
    client, _ = web
    assert client.get("/report/session/999.csv").status_code == 404
