"""Manifest parsing and reconciliation: scanned vs expected pallet IDs.

The manifest is a CSV of expected pallet payloads (known outbound pallets);
reconciling it against scanned payloads yields the *true* read rate for the
trial — misses that never produced motion, or pallets that never passed,
become visible here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

#: First-cell values (lowercased) that mark row 1 as a header, not data.
_HEADER_CELLS = frozenset({"payload", "pallet_id", "pallet", "id", "code"})


def parse_manifest(text: str) -> list[str]:
    """Expected payloads from CSV text: first column, optional header row,
    deduplicated preserving order. Tolerates CRLF, blank lines, and a UTF-8
    BOM (Excel's "CSV UTF-8" export writes one; left in place it would turn
    the header row into a phantom expected payload). Raises ``ValueError``
    on text the csv module cannot parse (e.g. binary uploads)."""
    text = text.lstrip("\ufeff")
    try:
        rows = [r for r in csv.reader(io.StringIO(text)) if r and r[0].strip()]
    except csv.Error as exc:
        raise ValueError(f"invalid CSV: {exc}") from exc
    if rows and rows[0][0].strip().lower() in _HEADER_CELLS:
        rows = rows[1:]
    seen: set[str] = set()
    payloads: list[str] = []
    for row in rows:
        payload = row[0].strip()
        if payload not in seen:
            seen.add(payload)
            payloads.append(payload)
    return payloads


@dataclass(slots=True)
class ManifestReconciliation:
    """Expected-vs-scanned buckets plus the trial's true read rate."""

    expected: int
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # expected, never scanned
    unexpected: list[str] = field(default_factory=list)  # scanned, not expected

    @property
    def true_read_rate(self) -> float | None:
        """Matched / expected — None when there is nothing expected."""
        return len(self.matched) / self.expected if self.expected else None

    def to_dict(self) -> dict:
        return {
            "expected": self.expected,
            "matched": self.matched,
            "missing": self.missing,
            "unexpected": self.unexpected,
            "true_read_rate": self.true_read_rate,
        }


def reconcile(expected: list[str], scanned: list[str]) -> ManifestReconciliation:
    """Bucket expected vs scanned payloads (order-preserving)."""
    scanned_set = set(scanned)
    expected_set = set(expected)
    seen: set[str] = set()
    unexpected = []
    for payload in scanned:
        if payload not in expected_set and payload not in seen:
            seen.add(payload)
            unexpected.append(payload)
    return ManifestReconciliation(
        expected=len(expected),
        matched=[p for p in expected if p in scanned_set],
        missing=[p for p in expected if p not in scanned_set],
        unexpected=unexpected,
    )
