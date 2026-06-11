"""Localhost echo stub for the HTTP sink (the TBD internal REST endpoint).

Accepts ``POST /events`` and replies ``{"ok": true}``. Optional chaos knobs
as query params for manual testing of the store-and-forward path:

    ?fail_rate=0.3    -> 30% of requests return HTTP 500
    ?latency_ms=250   -> delay each response by 250 ms

Point the sink at it via config:

    sinks:
      http: {enabled: true, url: "http://127.0.0.1:8808/events?fail_rate=0.2"}

Run:  python tools/echo_server.py [--port 8808]

(The pytest suite does NOT use this — tests run a stdlib http.server
fixture with scripted responses; this stub is for manual/chaos runs.)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("echo_server")

app = FastAPI(title="palletscan echo stub")
received_count = 0


@app.post("/events")
async def events(
    request: Request, fail_rate: float = 0.0, latency_ms: float = 0.0
) -> dict:
    global received_count
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000.0)
    if fail_rate > 0 and random.random() < fail_rate:
        raise HTTPException(status_code=500, detail="injected failure")
    body = await request.json()
    received_count += 1
    log.info(
        "event %d: kind=%s event_id=%s payload=%s",
        received_count,
        body.get("kind"),
        body.get("event_id"),
        body.get("payload"),
    )
    return {"ok": True, "received": received_count}


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8808)
    args = ap.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
