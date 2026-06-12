"""Reporting math, manifest parsing/reconciliation, and renderers."""

from __future__ import annotations

import pytest

from palletscan.reporting.ab import compute_ab_report
from palletscan.reporting.manifest import parse_manifest, reconcile
from palletscan.reporting.render import ab_csv, ab_markdown, reconciliation_csv


def _pass_row(
    payload: str,
    detail: dict[str, dict] | None,
    cameras: dict[str, int] | None = None,
    wall_time: str = "2026-06-11T10:00:00+00:00",
) -> dict:
    return {
        "kind": "pass",
        "payload": payload,
        "wall_time_iso": wall_time,
        "cameras": cameras or (
            {cam: d["decode_count"] for cam, d in detail.items()} if detail else {}
        ),
        "camera_detail": detail,
    }


def _detail(first_seen: float, first_decode: float, count: int) -> dict:
    return {
        "first_seen_ts": first_seen,
        "first_decode_ts": first_decode,
        "last_seen_ts": first_seen + 1.0,
        "decode_count": count,
    }


def _miss_row(
    source_id: str, wall_time: str = "2026-06-11T10:00:00+00:00"
) -> dict:
    return {"kind": "miss", "source_id": source_id, "wall_time_iso": wall_time}


def test_ab_math_seen_decoded_rate() -> None:
    # camA decodes 8 of 10 business passes and misses 2 of them itself;
    # camB decodes all 10.
    passes = []
    for i in range(10):
        detail = {"camB": _detail(float(i), float(i) + 0.2, 4)}
        if i < 8:
            detail["camA"] = _detail(float(i), float(i) + 0.5, 2)
        passes.append(_pass_row(f"PLT-{i}", detail))
    misses = [_miss_row("camA"), _miss_row("camA")]
    report = compute_ab_report(passes, misses)
    a, b = report.cameras["camA"], report.cameras["camB"]
    assert a.passes_seen == 10  # 8 decoded + its own 2 misses
    assert a.passes_decoded == 8
    assert a.read_rate == pytest.approx(0.8)
    assert a.misses == 2
    assert a.decodes_per_pass == pytest.approx(2.0)
    assert b.passes_seen == 10
    assert b.passes_decoded == 10
    assert b.read_rate == pytest.approx(1.0)
    assert b.misses == 0
    assert report.business_passes == 10
    assert report.business_misses == 2


def test_ab_ttfd_same_camera_deltas() -> None:
    passes = [
        _pass_row("PLT-1", {"camA": _detail(10.0, 10.3, 1)}),
        _pass_row("PLT-2", {"camA": _detail(20.0, 20.5, 1)}),
        _pass_row("PLT-3", {"camA": _detail(30.0, 30.7, 1)}),
    ]
    report = compute_ab_report(passes, [])
    cam = report.cameras["camA"]
    assert cam.ttfd_samples == pytest.approx([0.3, 0.5, 0.7])
    assert cam.ttfd_median_s == pytest.approx(0.5)
    assert cam.ttfd_p95_s == pytest.approx(0.7)


def test_ab_window_filters_use_real_datetimes() -> None:
    passes = [
        _pass_row("PLT-early", {"camA": _detail(0, 0.1, 1)},
                  wall_time="2026-06-11T08:00:00+00:00"),
        _pass_row("PLT-in", {"camA": _detail(0, 0.1, 1)},
                  wall_time="2026-06-11T10:30:00+00:00"),
        # Same instant as 10:30Z expressed in another offset: a string
        # compare would misorder this; fromisoformat must not.
        _pass_row("PLT-offset", {"camA": _detail(0, 0.1, 1)},
                  wall_time="2026-06-11T12:30:00+02:00"),
        _pass_row("PLT-late", {"camA": _detail(0, 0.1, 1)},
                  wall_time="2026-06-11T13:00:00+00:00"),
    ]
    report = compute_ab_report(
        passes,
        [],
        window_from="2026-06-11T10:00:00+00:00",
        window_to="2026-06-11T11:00:00+00:00",
    )
    assert report.business_passes == 2  # PLT-in and PLT-offset
    assert report.cameras["camA"].passes_decoded == 2


def test_ab_window_accepts_naive_bounds_as_utc() -> None:
    """Naive datetime-local input must filter, not raise TypeError against
    the always-aware stored stamps (adversarial-review finding)."""
    passes = [
        _pass_row("PLT-in", {"camA": _detail(0, 0.1, 1)},
                  wall_time="2026-06-11T10:30:00+00:00"),
        _pass_row("PLT-late", {"camA": _detail(0, 0.1, 1)},
                  wall_time="2026-06-11T13:00:00+00:00"),
    ]
    report = compute_ab_report(
        passes, [], window_from="2026-06-11T10:00", window_to="2026-06-11T11:00"
    )
    assert report.business_passes == 1
    date_only = compute_ab_report(passes, [], window_from="2026-06-11")
    assert date_only.business_passes == 2


def test_ab_legacy_rows_fall_back_to_cameras_map() -> None:
    legacy = _pass_row("PLT-old", None, cameras={"camA": 4})
    report = compute_ab_report([legacy], [])
    cam = report.cameras["camA"]
    assert cam.passes_seen == cam.passes_decoded == 1
    assert cam.decode_count == 4
    assert cam.ttfd_median_s is None  # no timing detail on old rows
    assert cam.read_rate == 1.0


def test_ab_empty_rows() -> None:
    report = compute_ab_report([], [])
    assert report.cameras == {}
    assert report.business_passes == 0


# -- manifest -----------------------------------------------------------------


def test_parse_manifest_header_crlf_dupes() -> None:
    text = "Pallet_ID\r\nPLT-1\r\nPLT-2\r\n\r\nPLT-1\r\nPLT-3\r\n"
    assert parse_manifest(text) == ["PLT-1", "PLT-2", "PLT-3"]


def test_parse_manifest_no_header_and_extra_columns() -> None:
    text = "PLT-9,zoneA\nPLT-8,zoneB\n"
    assert parse_manifest(text) == ["PLT-9", "PLT-8"]  # first column only
    assert parse_manifest("") == []
    # 'payload'-like first cell only counts as header on row 1
    assert parse_manifest("code\nid\n") == ["id"]


def test_parse_manifest_strips_utf8_bom() -> None:
    """Excel's 'CSV UTF-8' export writes a BOM; the header rule must still
    fire (adversarial-review finding: phantom expected payload otherwise)."""
    raw = b"\xef\xbb\xbfpayload\r\nPLT-1\r\nPLT-2\r\n"
    assert parse_manifest(raw.decode("utf-8")) == ["PLT-1", "PLT-2"]


def test_parse_manifest_rejects_unparseable_csv() -> None:
    huge_field = '"' + "x" * 200_000  # exceeds csv's field-size limit
    with pytest.raises(ValueError, match="invalid CSV"):
        parse_manifest(huge_field)


def test_reconcile_buckets_and_true_read_rate() -> None:
    rec = reconcile(
        expected=["PLT-1", "PLT-2", "PLT-3", "PLT-4"],
        scanned=["PLT-2", "PLT-4", "PLT-9", "PLT-9"],
    )
    assert rec.matched == ["PLT-2", "PLT-4"]
    assert rec.missing == ["PLT-1", "PLT-3"]
    assert rec.unexpected == ["PLT-9"]
    assert rec.true_read_rate == pytest.approx(0.5)
    assert reconcile([], ["PLT-1"]).true_read_rate is None


# -- renderers ----------------------------------------------------------------


def _sample_report():
    passes = [
        _pass_row(
            "PLT-1",
            {"camA": _detail(0.0, 0.4, 3), "camB": _detail(0.0, 0.2, 5)},
        ),
        _pass_row("PLT-2", {"camB": _detail(5.0, 5.1, 2)}),
    ]
    return compute_ab_report(passes, [_miss_row("camA")])


def test_ab_markdown_structure() -> None:
    text = ab_markdown(_sample_report())
    assert "# PalletScan A/B trial report" in text
    assert "| metric | camA | camB |" in text
    assert "| read rate | 50.0% | 100.0% |" in text
    assert "Highest read rate: **camB**" in text


def test_ab_csv_structure() -> None:
    lines = ab_csv(_sample_report()).strip().splitlines()
    assert lines[0].startswith("camera,passes_seen,passes_decoded,read_rate")
    rows = {line.split(",")[0]: line.split(",") for line in lines[1:]}
    assert rows["camA"][1] == "2" and rows["camA"][2] == "1"
    assert rows["camB"][1] == "2" and rows["camB"][2] == "2"
    assert float(rows["camA"][3]) == pytest.approx(0.5)


def test_reconciliation_csv_structure() -> None:
    rec = reconcile(["PLT-1", "PLT-2"], ["PLT-2", "PLT-X"])
    lines = reconciliation_csv(rec).strip().splitlines()
    assert lines[0] == "payload,status"
    assert "PLT-2,matched" in lines
    assert "PLT-1,missing" in lines
    assert "PLT-X,unexpected" in lines
