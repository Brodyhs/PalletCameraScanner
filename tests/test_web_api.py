"""Dashboard HTTP API: stats envelope contract, events, miss gallery +
review, evidence static serving and degradation, manifest upload."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from palletscan.config import MetricsConfig, WebConfig
from palletscan.events.sinks import SqliteSink
from palletscan.metrics import MetricsRegistry
from palletscan.types import MissEvent, PassEvent, Symbology
from palletscan.web.app import DashboardContext, create_app
from palletscan.web.store import ReadStore
from tests.test_metrics import SNAPSHOT_KEYS


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


def _miss(event_id: str, evidence_dir: Path) -> MissEvent:
    return MissEvent(
        candidate_id="camA-000002",
        source_id="camA",
        start_ts=5.0,
        end_ts=6.0,
        first_frame=150,
        last_frame=180,
        evidence_dir=str(evidence_dir),
        evidence_frame_count=2,
        event_id=event_id,
        wall_time_iso="2026-06-11T00:00:01+00:00",
    )


def _write_evidence(directory: Path, count: int = 2) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    image = np.full((40, 60), 128, np.uint8)
    for i in range(count):
        assert cv2.imwrite(str(directory / f"frame_{i:08d}.jpg"), image)


@pytest.fixture()
def web(tmp_path: Path):
    """Seeded app: 2 passes + 1 miss with real evidence on disk."""
    evidence_root = tmp_path / "evidence"
    miss_dir = evidence_root / "camA" / "2026-06-11" / "camA-000002"
    _write_evidence(miss_dir)
    db = tmp_path / "events.db"
    sink = SqliteSink(db)
    sink.handle(_pass("PLT-000001", "ev-p1"))
    sink.handle(_miss("ev-m1", miss_dir))
    sink.handle(_pass("PLT-000002", "ev-p2"))
    sink.close()
    metrics = MetricsRegistry(MetricsConfig())
    ctx = DashboardContext(
        snapshots={"camA": metrics.snapshot},
        previews={},
        business=None,
        store=ReadStore(db),
        evidence_root=evidence_root,
        web=WebConfig(),
    )
    return TestClient(create_app(ctx)), ctx, tmp_path


def test_index_and_static_served(web) -> None:
    client, _, _ = web
    resp = client.get("/")
    assert resp.status_code == 200
    assert "PalletScan" in resp.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_stats_envelope_pins_snapshot_contract(web) -> None:
    client, _, _ = web
    body = client.get("/stats.json").json()
    # D3 envelope: stable keys, snapshot served verbatim per camera.
    assert set(body) == {"generated_utc", "cameras", "business"}
    assert set(body["cameras"]) == {"camA"}
    assert set(body["cameras"]["camA"]) == SNAPSHOT_KEYS
    assert body["business"] is None  # single-camera mode


def test_stats_business_section_when_stationed(web) -> None:
    _, ctx, _ = web
    counters = {"passes_emitted": 7, "cross_camera_merges": 3}
    ctx.business = lambda: counters
    client = TestClient(create_app(ctx))
    assert client.get("/stats.json").json()["business"] == counters


def test_events_newest_first_with_kind_and_limit(web) -> None:
    client, _, _ = web
    events = client.get("/api/events").json()
    assert [e["event_id"] for e in events] == ["ev-p2", "ev-m1", "ev-p1"]
    passes = client.get("/api/events", params={"kind": "pass"}).json()
    assert {e["kind"] for e in passes} == {"pass"}
    one = client.get("/api/events", params={"limit": 1}).json()
    assert len(one) == 1
    # An absurd limit clamps rather than erroring.
    assert client.get("/api/events", params={"limit": 10_000}).status_code == 200


def test_miss_gallery_serves_evidence_images(web) -> None:
    client, _, _ = web
    misses = client.get("/api/misses").json()
    assert len(misses) == 1
    images = misses[0]["images"]
    assert len(images) == 2
    assert all(url.startswith("/evidence/") for url in images)
    image = client.get(images[0])
    assert image.status_code == 200
    assert image.content[:2] == b"\xff\xd8"  # actual JPEG bytes


def test_review_round_trip_persists(web, tmp_path: Path) -> None:
    client, _, _ = web
    resp = client.post(
        "/api/misses/ev-m1/review", json={"note": "operator confirmed empty"}
    )
    assert resp.status_code == 200
    misses = client.get("/api/misses").json()
    assert misses[0]["reviewed"] is True
    assert misses[0]["review_note"] == "operator confirmed empty"
    assert client.get("/api/misses", params={"unreviewed_only": True}).json() == []
    # Persists for a brand-new store on the same DB (dashboard restart).
    fresh = ReadStore(tmp_path / "events.db")
    assert fresh.misses()[0]["reviewed"] is True
    # Body-less POST defaults to reviewed=true (idempotent re-mark).
    assert client.post("/api/misses/ev-m1/review").status_code == 200


def test_pruned_evidence_degrades_not_500(web) -> None:
    client, _, tmp_path = web
    import shutil

    shutil.rmtree(tmp_path / "evidence" / "camA")
    misses = client.get("/api/misses").json()
    assert misses[0]["images"] == []  # row survives, gallery degrades


def test_relative_evidence_dir_resolves_from_any_cwd(
    web, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7e4c22c review, finding 3 (reproduced there): a run with the default
    relative evidence.dir stores cwd-relative paths; reviewing the trial
    from a different cwd rendered 'evidence pruned' for every miss even
    though the files exist under the dashboard's evidence root."""
    client, ctx, _ = web
    _write_evidence(ctx.evidence_root / "camA" / "2026-06-11" / "camA-000777")
    stored = Path("data/evidence") / "camA" / "2026-06-11" / "camA-000777"
    sink = SqliteSink(tmp_path / "events.db")
    sink.handle(_miss("ev-m-rel", stored))
    sink.close()
    monkeypatch.chdir(tmp_path)  # dashboard cwd != the run's cwd
    misses = client.get("/api/misses").json()
    rel = next(m for m in misses if m["event_id"] == "ev-m-rel")
    assert len(rel["images"]) == 2, "existing evidence reported as pruned"
    assert rel["images"][0].startswith("/evidence/camA/2026-06-11/camA-000777/")
    image = client.get(rel["images"][0])
    assert image.status_code == 200
    assert image.content[:2] == b"\xff\xd8"


def test_miss_image_urls_percent_encode_special_camera_ids(
    web, tmp_path: Path
) -> None:
    """Finding 13: camera ids only have to be non-empty, so a legal id like
    'cam#1' must not produce URLs truncated at the fragment marker."""
    client, ctx, _ = web
    burst = ctx.evidence_root / "cam#1" / "2026-06-11" / "cam#1-000123"
    _write_evidence(burst)
    sink = SqliteSink(tmp_path / "events.db")
    sink.handle(_miss("ev-m-hash", burst))
    sink.close()
    misses = client.get("/api/misses").json()
    row = next(m for m in misses if m["event_id"] == "ev-m-hash")
    assert row["images"]
    assert all("%23" in url and "#" not in url for url in row["images"])
    image = client.get(row["images"][0])
    assert image.status_code == 200
    assert image.content[:2] == b"\xff\xd8"


def test_evidence_dir_outside_root_yields_no_images(web, tmp_path: Path) -> None:
    client, ctx, _ = web
    outside = tmp_path / "elsewhere" / "burst"
    _write_evidence(outside)
    sink = SqliteSink(tmp_path / "events.db")
    sink.handle(_miss("ev-m2", outside))
    sink.close()
    misses = client.get("/api/misses").json()
    rogue = next(m for m in misses if m["event_id"] == "ev-m2")
    assert rogue["images"] == []  # path traversal cannot escape the mount


def test_manifest_raw_csv_upload_round_trip(web) -> None:
    client, _, _ = web
    body = "payload\r\nPLT-000001\r\nPLT-000009\r\nPLT-000001\r\n"
    resp = client.post(
        "/api/manifest", content=body, headers={"Content-Type": "text/csv"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"stored": 2}  # header skipped, dupe collapsed
    manifest = client.get("/api/manifest").json()
    assert manifest["count"] == 2
    assert manifest["payloads"] == ["PLT-000001", "PLT-000009"]
    assert client.post("/api/manifest", content="").status_code == 400
    # Excel-style BOM: the header rule must still fire.
    bom = b"\xef\xbb\xbfpayload\r\nPLT-000777\r\n"
    assert client.post("/api/manifest", content=bom).json() == {"stored": 1}
    assert client.get("/api/manifest").json()["payloads"] == ["PLT-000777"]
    # Unparseable (binary-ish) upload is a 400, not a 500.
    garbage = b'"' + b"x" * 200_000
    assert client.post("/api/manifest", content=garbage).status_code == 400


def test_manifest_upload_rejects_non_utf8_bytes(web) -> None:
    """Finding 11 (reproduced in the review): a cp1252 Excel export must be
    a clear 400 — errors='replace' silently stored U+FFFD-mangled payloads
    that can never match a scan, while reporting success. A rejected
    upload must also never replace the stored manifest."""
    client, _, _ = web
    ok = client.post("/api/manifest", content="payload\nPLT-OK\n")
    assert ok.status_code == 200
    bad = "payload\r\nPLT-MÜNCHEN-01\r\n".encode("cp1252")
    resp = client.post(
        "/api/manifest", content=bad, headers={"Content-Type": "text/csv"}
    )
    assert resp.status_code == 400
    assert "UTF-8" in resp.json()["detail"]
    # Never silent replacement: the previous manifest survives intact.
    assert client.get("/api/manifest").json()["payloads"] == ["PLT-OK"]
    # The same payload as valid UTF-8 round-trips with the umlaut intact.
    good = client.post(
        "/api/manifest", content="payload\nPLT-MÜNCHEN-01\n".encode()
    )
    assert good.status_code == 200
    assert client.get("/api/manifest").json()["payloads"] == ["PLT-MÜNCHEN-01"]


def test_live_503_when_no_previews(web) -> None:
    client, _, _ = web
    resp = client.get("/live/camA")
    assert resp.status_code == 503


# -- report endpoints (Step 6 wiring) -------------------------------------------


def test_report_ab_endpoint_math(web) -> None:
    client, _, _ = web
    report = client.get("/api/report/ab").json()
    cam = report["cameras"]["camA"]
    # Seeded: 2 decoded passes + 1 miss, all camA.
    assert cam["passes_seen"] == 3
    assert cam["passes_decoded"] == 2
    assert cam["read_rate"] == pytest.approx(2 / 3)
    assert cam["ttfd_median_s"] == pytest.approx(0.5)  # 1.5 - 1.0
    assert cam["misses"] == 1
    assert report["business"] == {"passes": 2, "misses": 1}
    # Window excluding everything -> empty report, not an error.
    empty = client.get(
        "/api/report/ab", params={"window_to": "2020-01-01T00:00:00+00:00"}
    ).json()
    assert empty["cameras"] == {}
    bad = client.get("/api/report/ab", params={"window_from": "not-a-date"})
    assert bad.status_code == 422
    # Naive datetime-local input (no offset) is valid and taken as UTC —
    # it must not 500 against the always-aware stored stamps.
    naive = client.get(
        "/api/report/ab", params={"window_from": "2026-06-11T00:00"}
    )
    assert naive.status_code == 200
    assert naive.json()["business"]["passes"] == 2


def test_report_downloads_have_disposition(web) -> None:
    client, _, _ = web
    md = client.get("/report/ab.md")
    assert md.status_code == 200
    assert "attachment" in md.headers["content-disposition"]
    assert "| metric | camA |" in md.text
    csv_resp = client.get("/report/ab.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.text.splitlines()[0].startswith("camera,passes_seen")
    assert "camA,3,2," in csv_resp.text


def test_reconciliation_endpoint_lifecycle(web) -> None:
    client, _, _ = web
    # 404 until a manifest exists (upload or config path).
    assert client.get("/api/report/reconciliation").status_code == 404
    assert client.get("/report/reconciliation.csv").status_code == 404
    body = "payload\nPLT-000001\nPLT-000002\nPLT-GHOST\n"
    assert client.post("/api/manifest", content=body).status_code == 200
    rec = client.get("/api/report/reconciliation").json()
    assert rec["expected"] == 3
    assert rec["matched"] == ["PLT-000001", "PLT-000002"]
    assert rec["missing"] == ["PLT-GHOST"]
    assert rec["unexpected"] == []
    assert rec["true_read_rate"] == pytest.approx(2 / 3)
    csv_resp = client.get("/report/reconciliation.csv")
    assert "PLT-GHOST,missing" in csv_resp.text
