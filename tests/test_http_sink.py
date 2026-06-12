"""HttpSink: outbox, drain, backoff/recovery, restart persistence, caps.

The fixture is a stdlib http.server thread with scriptable behavior (200s,
500s, connection-refused phases) — pytest does not depend on uvicorn; the
FastAPI echo stub in tools/ is for manual chaos testing only.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from palletscan.app import PipelineRunner
from palletscan.config import AppConfig, HttpSinkConfig, RetryConfig
from palletscan.events.http_sink import HttpSink
from palletscan.sources.factory import create_source
from palletscan.types import PassEvent, Symbology, now_iso

FAST_RETRY = RetryConfig(base_s=0.05, cap_s=0.2)


class ScriptedServer:
    """Echo server whose response mode tests flip at will."""

    def __init__(self, port: int = 0) -> None:
        self.received: list[dict] = []
        self.headers_seen: list[dict] = []
        self.mode = "ok"  # "ok" | "fail500" | "redirect"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                if outer.mode == "fail500":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"injected failure")
                    return
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", f"{outer.url}-elsewhere")
                    self.end_headers()
                    return
                outer.received.append(body)
                outer.headers_seen.append(dict(self.headers))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/events"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def server():
    s = ScriptedServer()
    yield s
    s.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sink_cfg(url: str, outbox: Path, **overrides) -> HttpSinkConfig:
    return HttpSinkConfig(
        enabled=True, url=url, outbox_path=outbox, retry=FAST_RETRY, **overrides
    )


def _pass_event(i: int) -> PassEvent:
    return PassEvent(
        payload=f"PLT-{i:06d}",
        symbology=Symbology.QR,
        first_seen_ts=float(i),
        last_seen_ts=float(i) + 0.5,
        decode_count=3,
        cameras={"synth0": 3},
        best_frame=("synth0", i),
        candidate_ids=[f"cand-{i}"],
        event_id=f"evt-{i:04d}",
        wall_time_iso=now_iso(),
    )


def _wait_until(cond: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _outbox_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT event_id FROM outbox ORDER BY seq")]
    finally:
        conn.close()


def test_happy_path_delivers_all_in_order(server: ScriptedServer, tmp_path: Path) -> None:
    cfg = _sink_cfg(server.url, tmp_path / "outbox.db", headers={"X-Api-Key": "k1"})
    sink = HttpSink(cfg)
    try:
        for i in range(5):
            sink.handle(_pass_event(i))
        assert _wait_until(lambda: sink.delivered == 5)
        assert [b["event_id"] for b in server.received] == [
            f"evt-{i:04d}" for i in range(5)
        ]
        assert server.received[0]["kind"] == "pass"
        assert server.received[0]["payload"] == "PLT-000000"
        assert all(h.get("X-Api-Key") == "k1" for h in server.headers_seen)
        stats = sink.outbox_stats()
        assert stats["depth"] == 0 and stats["dropped"] == 0
    finally:
        sink.close()


def test_500s_back_off_then_recover(server: ScriptedServer, tmp_path: Path) -> None:
    server.mode = "fail500"
    sink = HttpSink(_sink_cfg(server.url, tmp_path / "outbox.db"))
    try:
        for i in range(3):
            sink.handle(_pass_event(i))
        # Failures accrue while the endpoint 500s; nothing is delivered or lost.
        assert _wait_until(lambda: sink.upload_failures >= 3)
        assert sink.delivered == 0
        assert sink.outbox_stats()["depth"] == 3
        server.mode = "ok"
        assert _wait_until(lambda: sink.delivered == 3)
        assert sink.outbox_stats()["depth"] == 0
        assert len(server.received) == 3
    finally:
        sink.close()


def test_redirects_are_failures_not_acks(server: ScriptedServer, tmp_path: Path) -> None:
    """urllib would follow a 3xx by re-issuing a body-less GET — the event
    would be counted delivered without the receiver ever seeing it."""
    server.mode = "redirect"
    sink = HttpSink(_sink_cfg(server.url, tmp_path / "outbox.db"))
    try:
        sink.handle(_pass_event(0))
        assert _wait_until(lambda: sink.upload_failures >= 2)
        assert sink.delivered == 0
        assert sink.outbox_stats()["depth"] == 1  # still queued, not lost
        server.mode = "ok"
        assert _wait_until(lambda: sink.delivered == 1)
        assert [b["event_id"] for b in server.received] == ["evt-0000"]
    finally:
        sink.close()


def test_offline_accumulates_then_drains_when_endpoint_appears(tmp_path: Path) -> None:
    port = _free_port()
    sink = HttpSink(_sink_cfg(f"http://127.0.0.1:{port}/events", tmp_path / "o.db"))
    server = None
    try:
        for i in range(4):
            sink.handle(_pass_event(i))
        assert _wait_until(lambda: sink.upload_failures >= 2)  # refused, backing off
        assert sink.outbox_stats()["depth"] == 4
        server = ScriptedServer(port=port)
        assert _wait_until(lambda: sink.delivered == 4)
        assert [b["event_id"] for b in server.received] == [
            f"evt-{i:04d}" for i in range(4)
        ]
    finally:
        sink.close()
        if server is not None:
            server.stop()


def test_pending_rows_survive_restart_and_drain(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.db"
    dead_url = f"http://127.0.0.1:{_free_port()}/events"
    first = HttpSink(_sink_cfg(dead_url, outbox))
    try:
        for i in range(5):
            first.handle(_pass_event(i))
        assert _wait_until(lambda: first.upload_failures >= 1)
    finally:
        first.close()  # stops after the in-flight attempt; rows persist

    assert _outbox_rows(outbox) == [f"evt-{i:04d}" for i in range(5)]

    server = ScriptedServer()
    # No new handle() calls: the backlog must drain purely on start.
    second = HttpSink(_sink_cfg(server.url, outbox))
    try:
        assert _wait_until(lambda: second.delivered == 5)
        assert [b["event_id"] for b in server.received] == [
            f"evt-{i:04d}" for i in range(5)
        ]
        assert second.outbox_stats()["depth"] == 0
    finally:
        second.close()
        server.stop()


def test_size_cap_prunes_oldest_and_counts(tmp_path: Path) -> None:
    dead_url = f"http://127.0.0.1:{_free_port()}/events"
    body_len = len(json.dumps(__import__("dataclasses").asdict(_pass_event(0))))
    cap_events = 3
    max_mb = (body_len * cap_events + body_len // 2) / (1024 * 1024)
    sink = HttpSink(_sink_cfg(dead_url, tmp_path / "o.db", max_mb=max_mb))
    try:
        for i in range(10):
            sink.handle(_pass_event(i))
        stats = sink.outbox_stats()
        assert stats["depth"] <= cap_events
        assert stats["dropped"] == sink.dropped == 10 - stats["depth"]
        # Oldest went first: what remains is the tail of the sequence.
        remaining = _outbox_rows(tmp_path / "o.db")
        assert remaining == [f"evt-{i:04d}" for i in range(10 - len(remaining), 10)]
    finally:
        sink.close()


def test_age_cap_prunes_expired(tmp_path: Path) -> None:
    clock = {"t": 1_000_000.0}
    dead_url = f"http://127.0.0.1:{_free_port()}/events"
    sink = HttpSink(
        _sink_cfg(dead_url, tmp_path / "o.db", max_age_days=1.0),
        clock=lambda: clock["t"],
    )
    # Stop the uploader first: its first POST attempt holds the oldest row
    # in-flight, which the prune (correctly) skips — this test pins the
    # bus-thread prune logic, so make it deterministic.
    sink.close()
    sink.handle(_pass_event(0))
    clock["t"] += 86400.0 + 60.0  # one day and a minute later
    sink.handle(_pass_event(1))
    assert sink.dropped == 1
    assert _outbox_rows(tmp_path / "o.db") == ["evt-0001"]
    sink.close()


def test_pipeline_end_to_end_delivered_equals_emitted(
    server: ScriptedServer, fast_synth_config: AppConfig, tmp_path: Path
) -> None:
    """Echo-stub end-to-end: every emitted event is delivered exactly once
    across the run plus (if close() raced the drain) one restart."""
    sink_cfg = _sink_cfg(server.url, tmp_path / "outbox.db")
    sink = HttpSink(sink_cfg)
    runner = PipelineRunner(
        fast_synth_config, create_source(fast_synth_config), [sink]
    )
    summary = runner.run()  # close() stops the uploader; rows may remain
    assert summary.events_handled == 3 and summary.sink_errors == 0
    assert summary.metrics is not None and summary.metrics["outbox"] is not None

    if sink.outbox_stats()["depth"] > 0:
        drainer = HttpSink(sink_cfg)
        assert _wait_until(lambda: drainer.outbox_stats()["depth"] == 0)
        drainer.close()

    emitted_ids = sorted(e.event_id for e in runner.collected_events)
    assert sorted(b["event_id"] for b in server.received) == emitted_ids


# -- REVIEW_SYSTEM_0c30c77 finding b7 ------------------------------------------


def test_dropped_ledger_is_order_independent(tmp_path: Path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding b7 (repro: the delivered-after-prune
    reconciliation could run before the pruner's increment; the max(0,...)
    clamp absorbed the decrement and a DELIVERED event stayed permanently
    counted as dropped). The two adjustments must net identically in
    either order."""
    sink = HttpSink(
        HttpSinkConfig(
            enabled=True,
            url="http://127.0.0.1:1/events",  # never reached
            outbox_path=tmp_path / "outbox.db",
        )
    )
    try:
        # reconcile-before-prune: the old clamp turned (-1, +3) into 3.
        sink._adjust_dropped(-1)
        sink._adjust_dropped(3)
        assert sink.dropped == 2
        # prune-before-reconcile nets the same.
        sink._adjust_dropped(3)
        sink._adjust_dropped(-1)
        assert sink.dropped == 4
    finally:
        sink.close()


def test_dropped_counter_survives_two_thread_hammer(tmp_path: Path) -> None:
    """Finding b7, second arm: unsynchronized read-modify-writes from the
    bus and uploader threads lost updates in both directions."""
    sink = HttpSink(
        HttpSinkConfig(
            enabled=True,
            url="http://127.0.0.1:1/events",
            outbox_path=tmp_path / "outbox.db",
        )
    )
    try:
        n = 5000

        def add() -> None:
            for _ in range(n):
                sink._adjust_dropped(1)

        def sub() -> None:
            for _ in range(n):
                sink._adjust_dropped(-1)

        threads = [threading.Thread(target=add), threading.Thread(target=sub)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sink.dropped == 0, "lost updates on the dropped ledger"
    finally:
        sink.close()
