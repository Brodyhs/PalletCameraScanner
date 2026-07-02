"""A/B trial report: per-camera performance from stored event rows.

Pure functions over parsed ``detail_json`` rows (max-revision business
passes plus per-camera misses) so the math is unit-testable without a
database. Per spec §4, business passes dedupe across cameras while each
camera's stats count its own sightings:

- ``passes_seen``   = business passes whose ``camera_detail`` contains the
  camera, plus the camera's own misses (a miss is a seen-but-undecoded pass).
- ``passes_decoded``= business passes whose ``camera_detail`` contains the
  camera.
- time-to-first-decode uses same-camera timestamps
  (``first_decode_ts - first_seen_ts`` within one camera's detail), so
  cross-camera clock skew cancels.

Legacy rows (pre-Phase-4, no ``camera_detail``) fall back to the
``cameras`` map: the pass counts toward seen/decoded; ttfd is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from palletscan.metrics import percentile


def _parse_window(iso: str | None) -> datetime | None:
    """Window bound from a query string. Naive inputs (the natural
    ``datetime-local`` shapes like ``2026-06-11T08:00``) are taken as UTC —
    stored stamps are always offset-aware, and comparing naive against
    aware raises TypeError."""
    if not iso:
        return None
    stamp = datetime.fromisoformat(iso)
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


@dataclass(slots=True)
class CameraReport:
    passes_seen: int = 0
    passes_decoded: int = 0
    decode_count: int = 0
    misses: int = 0
    ttfd_samples: list[float] = field(default_factory=list)

    @property
    def read_rate(self) -> float | None:
        return self.passes_decoded / self.passes_seen if self.passes_seen else None

    @property
    def decodes_per_pass(self) -> float | None:
        return (
            self.decode_count / self.passes_decoded if self.passes_decoded else None
        )

    @property
    def ttfd_median_s(self) -> float | None:
        return percentile(sorted(self.ttfd_samples), 0.50)

    @property
    def ttfd_p95_s(self) -> float | None:
        return percentile(sorted(self.ttfd_samples), 0.95)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passes_seen": self.passes_seen,
            "passes_decoded": self.passes_decoded,
            "read_rate": self.read_rate,
            "ttfd_median_s": self.ttfd_median_s,
            "ttfd_p95_s": self.ttfd_p95_s,
            "decodes_per_pass": self.decodes_per_pass,
            "misses": self.misses,
        }


@dataclass(slots=True)
class ABReport:
    cameras: dict[str, CameraReport]
    business_passes: int
    business_misses: int
    window_from: str | None
    window_to: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cameras": {cam: r.to_dict() for cam, r in self.cameras.items()},
            "business": {
                "passes": self.business_passes,
                "misses": self.business_misses,
            },
            "window": {"from": self.window_from, "to": self.window_to},
        }


def _in_window(
    iso: str | None, t_from: datetime | None, t_to: datetime | None
) -> bool:
    """Real datetime comparison (string compare breaks across offsets)."""
    if iso is None or (t_from is None and t_to is None):
        return True
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return True  # unparseable stamps are included, never dropped
    if stamp.tzinfo is None:  # defensive: hand-edited rows
        stamp = stamp.replace(tzinfo=timezone.utc)
    if t_from is not None and stamp < t_from:
        return False
    return not (t_to is not None and stamp > t_to)


def rows_in_window(
    rows: list[dict[str, Any]],
    window_from: str | None = None,
    window_to: str | None = None,
) -> list[dict[str, Any]]:
    """The rows whose ``wall_time_iso`` falls inside the window — the same
    filter :func:`compute_ab_report` applies, exposed for session summaries
    (payload-level manifest reconciliation over a session's time box)."""
    t_from, t_to = _parse_window(window_from), _parse_window(window_to)
    return [
        r for r in rows if _in_window(r.get("wall_time_iso"), t_from, t_to)
    ]


def compute_ab_report(
    pass_rows: list[dict[str, Any]],
    miss_rows: list[dict[str, Any]],
    window_from: str | None = None,
    window_to: str | None = None,
) -> ABReport:
    """Build the per-camera comparison from parsed event detail rows."""
    t_from = _parse_window(window_from)
    t_to = _parse_window(window_to)
    cameras: dict[str, CameraReport] = {}

    def cam(source_id: str) -> CameraReport:
        return cameras.setdefault(source_id, CameraReport())

    business_passes = 0
    for row in pass_rows:
        if not _in_window(row.get("wall_time_iso"), t_from, t_to):
            continue
        business_passes += 1
        detail = row.get("camera_detail")
        if detail:
            for source_id, entry in detail.items():
                report = cam(source_id)
                report.passes_seen += 1
                report.passes_decoded += 1
                report.decode_count += int(entry.get("decode_count", 0))
                first_seen = entry.get("first_seen_ts")
                first_decode = entry.get("first_decode_ts")
                if first_seen is not None and first_decode is not None:
                    report.ttfd_samples.append(
                        max(0.0, float(first_decode) - float(first_seen))
                    )
        else:
            # Legacy pre-Phase-4 row: cameras map only, no timing detail.
            for source_id, count in (row.get("cameras") or {}).items():
                report = cam(source_id)
                report.passes_seen += 1
                report.passes_decoded += 1
                report.decode_count += int(count)

    business_misses = 0
    for row in miss_rows:
        if not _in_window(row.get("wall_time_iso"), t_from, t_to):
            continue
        business_misses += 1
        source_id = row.get("source_id")
        if source_id:
            report = cam(source_id)
            report.passes_seen += 1
            report.misses += 1

    return ABReport(
        cameras=dict(sorted(cameras.items())),
        business_passes=business_passes,
        business_misses=business_misses,
        window_from=window_from,
        window_to=window_to,
    )
