"""DashboardServer lifecycle + CLI dashboard plumbing."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml

import palletscan.cli as cli
from palletscan.config import MetricsConfig, WebConfig
from palletscan.events.sinks import SqliteSink
from palletscan.metrics import MetricsRegistry
from palletscan.types import PassEvent, Symbology
from palletscan.web.app import DashboardContext, create_app
from palletscan.web.server import DashboardServer, DashboardServerError
from palletscan.web.store import ReadStore


def _pass(payload: str = "PLT-000001") -> PassEvent:
    return PassEvent(
        payload=payload,
        symbology=Symbology.QR,
        first_seen_ts=1.0,
        last_seen_ts=2.0,
        decode_count=3,
        cameras={"camA": 3},
        best_frame=("camA", 42),
        candidate_ids=["camA-000001"],
        event_id=f"ev-{payload}",
        wall_time_iso="2026-06-11T00:00:00+00:00",
    )


def _ctx(tmp_path: Path) -> DashboardContext:
    db = tmp_path / "events.db"
    sink = SqliteSink(db)
    sink.handle(_pass())
    sink.close()
    metrics = MetricsRegistry(MetricsConfig())
    return DashboardContext(
        snapshots={"camA": metrics.snapshot},
        previews={},
        business=None,
        store=ReadStore(db),
        evidence_root=tmp_path / "evidence",
        web=WebConfig(),
    )


def test_server_lifecycle_smoke(tmp_path: Path) -> None:
    server = DashboardServer(create_app(_ctx(tmp_path)), "127.0.0.1", 0)
    server.start()
    try:
        assert server.port != 0  # ephemeral port resolved
        stats = httpx.get(f"{server.url}/stats.json", timeout=10)
        assert stats.status_code == 200
        assert "camA" in stats.json()["cameras"]
        events = httpx.get(f"{server.url}/api/events", timeout=10).json()
        assert [e["event_id"] for e in events] == ["ev-PLT-000001"]
    finally:
        server.stop()
    assert server._thread is None  # joined cleanly
    server.stop()  # idempotent
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{server.port}/stats.json", timeout=2)


def test_stop_is_bounded_with_connected_mjpeg_client(tmp_path: Path) -> None:
    """A dashboard tab watching /live must not stall shutdown: uvicorn's
    graceful shutdown is bounded by timeout_graceful_shutdown, so stop()
    joins promptly even with an open unbounded stream (adversarial-review
    finding)."""
    import socket
    import time

    import numpy as np

    from palletscan.config import WebConfig as _WebConfig
    from palletscan.types import Frame, MotionResult
    from palletscan.web.preview import LivePreview

    ctx = _ctx(tmp_path)
    preview = LivePreview("camA", _WebConfig())
    preview.update(
        Frame(
            image=np.full((60, 80), 128, np.uint8),
            ts=0.0,
            frame_index=0,
            source_id="camA",
        ),
        MotionResult(False, None, None, 0.0),
        [],
    )
    ctx.previews = {"camA": preview}
    server = DashboardServer(create_app(ctx), "127.0.0.1", 0)
    server.start()
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        sock.sendall(b"GET /live/camA HTTP/1.1\r\nHost: localhost\r\n\r\n")
        first = sock.recv(4096)  # headers + first multipart frame
        assert b"200" in first
        started = time.monotonic()
        server.stop(timeout_s=10.0)
        elapsed = time.monotonic() - started
        assert elapsed < 8.0, f"stop() stalled {elapsed:.1f}s with open stream"
        assert server._thread is None  # joined, not leaked
    finally:
        sock.close()


def test_start_after_stop_raises_single_use_guard(tmp_path: Path) -> None:
    """7e4c22c review, finding 17 (guarded, not fixed): stop() consumes the
    single-use uvicorn.Server, so a second start() used to report success
    while the serve thread died within a tick — every request then got
    connection refused. Until Phase 5 builds real restart machinery, the
    restart attempt must fail loudly instead."""
    server = DashboardServer(create_app(_ctx(tmp_path)), "127.0.0.1", 0)
    server.start()
    server.stop()
    with pytest.raises(DashboardServerError, match="single-use"):
        server.start()


def test_server_start_fails_fast_on_taken_port(tmp_path: Path) -> None:
    app = create_app(_ctx(tmp_path))
    first = DashboardServer(app, "127.0.0.1", 0)
    first.start()
    try:
        second = DashboardServer(app, "127.0.0.1", first.port)
        with pytest.raises(DashboardServerError):
            second.start(timeout_s=10)
    finally:
        first.stop()


def test_cli_synth_with_dashboard_serves_during_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """--dashboard on a synth run: server is reachable while running and
    stopped afterwards."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "web": {"port": 0},
                "sinks": {"console": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    seen: dict = {}
    original = cli._start_dashboard

    def spying_start(cfg, runners, business):
        server = original(cfg, runners, business)
        seen["stats"] = httpx.get(f"{server.url}/stats.json", timeout=10).json()
        seen["url"] = server.url
        seen["server"] = server
        return server

    monkeypatch.setattr(cli, "_start_dashboard", spying_start)
    code = cli.main(
        [
            "synth",
            "--passes",
            "1",
            "--config",
            str(cfg_path),
            "--data-dir",
            str(tmp_path / "data"),
            "--dashboard",
        ]
    )
    assert code == 0
    assert "dashboard serving on" in capsys.readouterr().out
    assert "synth0" in seen["stats"]["cameras"]
    assert seen["stats"]["business"] is None
    assert seen["server"]._thread is None  # stopped in the finally
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{seen['url']}/stats.json", timeout=2)


def test_cli_dashboard_port_in_use_is_clean_exit_2(tmp_path: Path, capsys) -> None:
    """Port collision surfaces as a clean message + exit 2, not a traceback
    (adversarial-review finding)."""
    blocker = DashboardServer(create_app(_ctx(tmp_path)), "127.0.0.1", 0)
    blocker.start()
    try:
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "web": {"port": blocker.port},
                    "sinks": {"console": {"enabled": False}},
                }
            ),
            encoding="utf-8",
        )
        code = cli.main(
            [
                "synth",
                "--passes",
                "1",
                "--config",
                str(cfg_path),
                "--data-dir",
                str(tmp_path / "data"),
                "--dashboard",
            ]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "synth:" in err and "dashboard" in err

        # Standalone subcommand: same clean failure.
        data = tmp_path / "data2"
        sink = SqliteSink(data / "palletscan.db")
        sink.handle(_pass())
        sink.close()
        code = cli.main(
            ["dashboard", "--config", str(cfg_path), "--data-dir", str(data)]
        )
        assert code == 2
        assert "dashboard:" in capsys.readouterr().err
    finally:
        blocker.stop()


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1000)() == 0,
    reason="root ignores file permission bits",
)
def test_cli_dashboard_readonly_db_is_clean_exit_2(tmp_path: Path, capsys) -> None:
    """Finding 4, path (1): a readonly events DB passes the is_file() check
    but cannot host the web tables — clean message + exit 2, not a raw
    sqlite3.OperationalError out of cli.main."""
    data = tmp_path / "data"
    sink = SqliteSink(data / "palletscan.db")
    sink.handle(_pass())
    sink.close()
    (data / "palletscan.db").chmod(0o444)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump({"web": {"port": 0}}), encoding="utf-8")
    try:
        code = cli.main(
            ["dashboard", "--config", str(cfg_path), "--data-dir", str(data)]
        )
    finally:
        (data / "palletscan.db").chmod(0o644)
    assert code == 2
    err = capsys.readouterr().err
    assert "dashboard:" in err and "cannot open events DB" in err


def test_cli_synth_dashboard_with_unborn_db_dir_succeeds(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Finding 4, path (2): a sinks.sqlite.path whose parent directory does
    not exist yet crashed --dashboard startup before the run began, while
    the same config without --dashboard worked (the sink mkdirs lazily).
    The ReadStore now creates it too."""
    monkeypatch.chdir(tmp_path)  # relative config paths land under tmp
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "web": {"port": 0},
                "sinks": {
                    "console": {"enabled": False},
                    "sqlite": {"path": "unborn/nested/palletscan.db"},
                },
            }
        ),
        encoding="utf-8",
    )
    code = cli.main(
        ["synth", "--passes", "1", "--config", str(cfg_path), "--dashboard"]
    )
    assert code == 0
    assert "dashboard serving on" in capsys.readouterr().out
    assert (tmp_path / "unborn" / "nested" / "palletscan.db").is_file()


def test_cli_dashboard_requires_existing_db(tmp_path: Path, capsys) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump({"web": {"port": 0}}), encoding="utf-8")
    code = cli.main(
        [
            "dashboard",
            "--config",
            str(cfg_path),
            "--data-dir",
            str(tmp_path / "nowhere"),
        ]
    )
    assert code == 2
    assert "events DB not found" in capsys.readouterr().err


def test_cli_dashboard_standalone_read_only(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    sink = SqliteSink(data / "palletscan.db")
    sink.handle(_pass("PLT-77"))
    sink.close()
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump({"web": {"port": 0}}), encoding="utf-8")

    probes: dict = {}

    def probe_instead_of_blocking() -> None:
        out = capsys.readouterr().out
        url = next(
            token
            for line in out.splitlines()
            if "dashboard serving on" in line
            for token in line.split()
            if token.startswith("http://")
        )
        probes["stats"] = httpx.get(f"{url}/stats.json", timeout=10).json()
        probes["events"] = httpx.get(f"{url}/api/events", timeout=10).json()
        probes["live"] = httpx.get(f"{url}/live/camA", timeout=10).status_code

    monkeypatch.setattr(cli, "_wait_for_interrupt", probe_instead_of_blocking)
    code = cli.main(
        ["dashboard", "--config", str(cfg_path), "--data-dir", str(data)]
    )
    assert code == 0
    assert probes["stats"]["cameras"] == {}  # no runners attached
    assert [e["payload"] for e in probes["events"]] == ["PLT-77"]
    assert probes["live"] == 503  # standalone mode


def test_dashboard_unavailable_without_sqlite_sink(tmp_path: Path, capsys) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {"web": {"port": 0}, "sinks": {"sqlite": {"enabled": False}}}
        ),
        encoding="utf-8",
    )
    code = cli.main(
        [
            "synth",
            "--passes",
            "1",
            "--config",
            str(cfg_path),
            "--data-dir",
            str(tmp_path / "data"),
            "--dashboard",
        ]
    )
    assert code == 2
    assert "sinks.sqlite" in capsys.readouterr().err
