"""Renderers: A/B report and reconciliation as markdown and CSV downloads."""

from __future__ import annotations

import csv
import io
from typing import Any

from palletscan.reporting.ab import ABReport
from palletscan.reporting.manifest import ManifestReconciliation


def _num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"


_ROWS: list[tuple[str, str]] = [
    ("passes seen", "passes_seen"),
    ("passes decoded", "passes_decoded"),
    ("read rate", "read_rate"),
    ("ttfd median (s)", "ttfd_median_s"),
    ("ttfd p95 (s)", "ttfd_p95_s"),
    ("decodes per pass", "decodes_per_pass"),
    ("misses", "misses"),
]


def ab_markdown(report: ABReport) -> str:
    """Camera-vs-camera comparison table (generic over source ids)."""
    data = report.to_dict()
    cameras = list(data["cameras"])
    lines = [
        "# PalletScan A/B trial report",
        "",
        f"Window: {data['window']['from'] or 'start'} → "
        f"{data['window']['to'] or 'now'}",
        f"Business passes (deduped): {data['business']['passes']}  ·  "
        f"misses: {data['business']['misses']}",
        "",
    ]
    if not cameras:
        lines.append("_No pass data in the window._")
        return "\n".join(lines) + "\n"
    lines.append("| metric | " + " | ".join(cameras) + " |")
    lines.append("|---" * (len(cameras) + 1) + "|")
    for label, key in _ROWS:
        cells = []
        for camera in cameras:
            value = data["cameras"][camera][key]
            cells.append(_pct(value) if key == "read_rate" else _num(value))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    best = max(
        cameras,
        key=lambda c: data["cameras"][c]["read_rate"] or 0.0,
    )
    lines += [
        "",
        f"Highest read rate: **{best}** "
        f"({_pct(data['cameras'][best]['read_rate'])}).",
        "",
        "Time-to-first-decode uses same-camera timestamps, so cross-camera "
        "clock skew does not affect it.",
    ]
    return "\n".join(lines) + "\n"


def ab_csv(report: ABReport) -> str:
    data = report.to_dict()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "camera",
            "passes_seen",
            "passes_decoded",
            "read_rate",
            "ttfd_median_s",
            "ttfd_p95_s",
            "decodes_per_pass",
            "misses",
        ]
    )
    for camera, r in data["cameras"].items():
        writer.writerow(
            [
                camera,
                r["passes_seen"],
                r["passes_decoded"],
                "" if r["read_rate"] is None else f"{r['read_rate']:.6f}",
                "" if r["ttfd_median_s"] is None else f"{r['ttfd_median_s']:.6f}",
                "" if r["ttfd_p95_s"] is None else f"{r['ttfd_p95_s']:.6f}",
                ""
                if r["decodes_per_pass"] is None
                else f"{r['decodes_per_pass']:.4f}",
                r["misses"],
            ]
        )
    return out.getvalue()


def reconciliation_csv(rec: ManifestReconciliation) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["payload", "status"])
    for payload in rec.matched:
        writer.writerow([payload, "matched"])
    for payload in rec.missing:
        writer.writerow([payload, "missing"])
    for payload in rec.unexpected:
        writer.writerow([payload, "unexpected"])
    return out.getvalue()


def session_csv(session: dict[str, Any]) -> str:
    """One operator session as CSV: the reconciliation block, then the
    per-camera table, then payload-level manifest matching when present.
    Works for open sessions too (live counts under the same keys)."""
    counts = session.get("counts") or {}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["field", "value"])
    for key in (
        "id",
        "status",
        "started_utc",
        "closed_utc",
        "expected_count",
        "ack_note",
    ):
        value = session.get(key)
        writer.writerow([key, "" if value is None else value])
    for key in ("decoded", "missed", "shortfall"):
        writer.writerow([key, _num(counts.get(key)) if counts else ""])
    cameras: dict[str, Any] = session.get("cameras") or {}
    if cameras:
        writer.writerow([])
        writer.writerow(["camera", "passes_seen", "passes_decoded", "misses"])
        for camera, r in cameras.items():
            writer.writerow(
                [camera, r["passes_seen"], r["passes_decoded"], r["misses"]]
            )
    manifest: dict[str, Any] | None = session.get("manifest")
    if manifest:
        writer.writerow([])
        writer.writerow(["payload", "status"])
        for payload in manifest.get("matched", []):
            writer.writerow([payload, "matched"])
        for payload in manifest.get("missing", []):
            writer.writerow([payload, "missing"])
        for payload in manifest.get("unexpected", []):
            writer.writerow([payload, "unexpected"])
    return out.getvalue()
