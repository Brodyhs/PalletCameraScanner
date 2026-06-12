"""Dashboard FastAPI app (factory; no globals, TestClient-friendly).

``create_app`` receives everything through :class:`DashboardContext`:
metrics snapshots and live previews are read in-process from the attached
runners; events/misses/reviews/manifest go through the SQLite
:class:`~palletscan.web.store.ReadStore`. The app works degraded by
design — no previews (standalone mode) means 503 on /live, pruned evidence
means a miss renders without images, never a 500.

Localhost-bound by default and unauthenticated (spec §12): do not expose
beyond the host.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from palletscan.config import WebConfig
from palletscan.reporting.ab import ABReport, compute_ab_report
from palletscan.reporting.manifest import (
    ManifestReconciliation,
    parse_manifest,
    reconcile,
)
from palletscan.reporting.render import ab_csv, ab_markdown, reconciliation_csv
from palletscan.types import now_iso
from palletscan.web.preview import LivePreview
from palletscan.web.store import ReadStore

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

#: Per-part boundary for the MJPEG stream.
_BOUNDARY = "palletscanframe"

#: Re-yield the last frame after this long without a new stamp, so proxies
#: and browsers keep the connection alive through idle periods.
_KEEPALIVE_S = 1.0

_MAX_EVENT_LIMIT = 500


@dataclass(slots=True)
class DashboardContext:
    """Everything the dashboard reads, injected by the CLI wiring."""

    snapshots: dict[str, Callable[[], dict[str, Any]]]
    previews: dict[str, LivePreview]
    business: Callable[[], dict[str, Any]] | None
    store: ReadStore
    evidence_root: Path
    web: WebConfig


class ReviewRequest(BaseModel):
    reviewed: bool = True
    note: str | None = None


def _mjpeg_part(data: bytes) -> bytes:
    return (
        f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
        f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n"
    )


def create_app(ctx: DashboardContext) -> FastAPI:
    app = FastAPI(title="PalletScan", docs_url=None, redoc_url=None)

    def _miss_images(evidence_dir: str) -> list[str]:
        """Evidence JPEGs as /evidence/... URLs; [] when pruned or outside
        the root — a stale miss row must degrade, never 500."""
        root = ctx.evidence_root.resolve()
        try:
            directory = Path(evidence_dir).resolve()
            rel = directory.relative_to(root)
        except (ValueError, OSError):
            return []
        try:
            names = sorted(p.name for p in directory.glob("*.jpg"))
        except OSError:
            return []
        return [f"/evidence/{rel.as_posix()}/{name}" for name in names]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/stats.json")
    def stats() -> dict[str, Any]:
        # D3 envelope, uniform across modes: the pinned per-runner
        # snapshot() dict is served verbatim under cameras[<source_id>].
        return {
            "generated_utc": now_iso(),
            "cameras": {sid: fn() for sid, fn in ctx.snapshots.items()},
            "business": ctx.business() if ctx.business is not None else None,
        }

    @app.get("/live/{camera_id}")
    async def live(camera_id: str, request: Request) -> StreamingResponse:
        if not ctx.previews:
            raise HTTPException(
                503, "no live runners attached (standalone dashboard)"
            )
        preview = ctx.previews.get(camera_id)
        if preview is None:
            raise HTTPException(
                404,
                f"unknown camera {camera_id!r}; "
                f"available: {sorted(ctx.previews)}",
            )
        interval = 1.0 / ctx.web.preview_fps

        async def frames():
            # Async generator on purpose: a sync generator would park an
            # AnyIO worker token per client across its pacing sleeps and
            # tear down nondeterministically on disconnect; this cancels at
            # the next await and only occupies the threadpool for the
            # milliseconds of an actual encode.
            last_stamp = -1
            last_yield = 0.0
            while not await request.is_disconnected():
                data, stamp = await run_in_threadpool(preview.render_jpeg)
                now = time.monotonic()
                if data is not None and (
                    stamp != last_stamp or now - last_yield >= _KEEPALIVE_S
                ):
                    last_stamp = stamp
                    last_yield = now
                    yield _mjpeg_part(data)
                await asyncio.sleep(interval)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        )

    @app.get("/api/events")
    def events(limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        return ctx.store.recent_events(
            limit=max(1, min(limit, _MAX_EVENT_LIMIT)), kind=kind
        )

    @app.get("/api/misses")
    def misses(
        limit: int = 50, unreviewed_only: bool = False
    ) -> list[dict[str, Any]]:
        rows = ctx.store.misses(
            limit=max(1, min(limit, _MAX_EVENT_LIMIT)),
            unreviewed_only=unreviewed_only,
        )
        for row in rows:
            row["images"] = _miss_images(row.get("evidence_dir", ""))
        return rows

    @app.post("/api/misses/{event_id}/review")
    def review(event_id: str, body: ReviewRequest | None = None) -> dict[str, Any]:
        body = body or ReviewRequest()
        # Reviews key on the event_id (not the evidence path) so they
        # survive evidence pruning; unknown ids upsert harmlessly.
        ctx.store.mark_reviewed(event_id, reviewed=body.reviewed, note=body.note)
        return {"event_id": event_id, "reviewed": body.reviewed}

    @app.get("/api/manifest")
    def manifest() -> dict[str, Any]:
        payloads = ctx.store.manifest_payloads()
        return {"count": len(payloads), "payloads": payloads}

    @app.post("/api/manifest")
    async def upload_manifest(request: Request) -> dict[str, Any]:
        # Raw text/csv body (JS FileReader -> fetch): no multipart parser
        # dependency (D7).
        body = await request.body()
        try:
            payloads = parse_manifest(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise HTTPException(400, f"manifest is not parseable CSV: {exc}")
        if not payloads:
            raise HTTPException(400, "no payloads found in manifest CSV")
        stored = await run_in_threadpool(ctx.store.replace_manifest, payloads)
        return {"stored": stored}

    # -- reporting -------------------------------------------------------------

    def _ab_report(window_from: str | None, window_to: str | None) -> ABReport:
        passes, miss_rows = ctx.store.pass_and_miss_rows()
        try:
            return compute_ab_report(
                passes, miss_rows, window_from=window_from, window_to=window_to
            )
        except ValueError as exc:  # unparseable from/to
            raise HTTPException(422, f"bad window timestamp: {exc}") from exc

    def _reconciliation() -> ManifestReconciliation:
        expected = ctx.store.manifest_payloads()
        if not expected:
            raise HTTPException(
                404,
                "no manifest loaded (POST /api/manifest or set "
                "report.manifest_path)",
            )
        scanned = [p["payload"] for p in ctx.store.pass_and_miss_rows()[0]]
        return reconcile(expected, scanned)

    @app.get("/api/report/ab")
    def report_ab(
        window_from: str | None = None, window_to: str | None = None
    ) -> dict[str, Any]:
        return _ab_report(window_from, window_to).to_dict()

    @app.get("/report/ab.md")
    def report_ab_md(
        window_from: str | None = None, window_to: str | None = None
    ) -> PlainTextResponse:
        return PlainTextResponse(
            ab_markdown(_ab_report(window_from, window_to)),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="ab-report.md"'
            },
        )

    @app.get("/report/ab.csv")
    def report_ab_csv(
        window_from: str | None = None, window_to: str | None = None
    ) -> PlainTextResponse:
        return PlainTextResponse(
            ab_csv(_ab_report(window_from, window_to)),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="ab-report.csv"'
            },
        )

    @app.get("/api/report/reconciliation")
    def report_reconciliation() -> dict[str, Any]:
        return _reconciliation().to_dict()

    @app.get("/report/reconciliation.csv")
    def report_reconciliation_csv() -> PlainTextResponse:
        return PlainTextResponse(
            reconciliation_csv(_reconciliation()),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="reconciliation.csv"'
            },
        )

    app.mount(
        "/evidence",
        StaticFiles(directory=ctx.evidence_root, check_dir=False),
        name="evidence",
    )
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
