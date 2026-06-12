"""Dashboard frontend regression tests: runs the node:vm JS harness in
tests/js against the real app.js + index.html, one case per confirmed
review finding (6, 7, 9, 10, 11, 14 of REVIEW_7e4c22c.md)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("node") is None:  # pragma: no cover - environment-dependent
    pytest.skip(
        "node not installed; JS dashboard tests need it", allow_module_level=True
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = REPO_ROOT / "tests" / "js" / "app_js_tests.mjs"

FINDING_TESTS = [
    "test_f6_poll_preserves_note_drafts",
    "test_f7_failed_review_surfaces_error",
    "test_f9_transient_report_error_keeps_panel",
    "test_f10_live_tile_reconnects_after_error",
    "test_f11_upload_sends_raw_bytes",
    "test_f14_manifest_upload_network_failure_feedback",
]


@pytest.mark.parametrize("name", FINDING_TESTS)
def test_dashboard_js(name: str) -> None:
    proc = subprocess.run(
        ["node", str(JS_TESTS), name],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"{name} failed (exit {proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
