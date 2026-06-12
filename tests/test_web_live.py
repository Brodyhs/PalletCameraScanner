"""Live MJPEG endpoint against a realtime synthetic runner.

Streams over a real uvicorn server on an ephemeral port: the installed
Starlette TestClient runs each ASGI request to completion before returning,
so an unbounded multipart stream can never be exercised through it — the
real server is also exactly what production serves.
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx

from palletscan.app import PipelineRunner
from palletscan.config import (
    AppConfig,
    ConsoleSinkConfig,
    SyntheticConfig,
    WebConfig,
    apply_overrides,
)
from palletscan.web.app import DashboardContext, create_app
from palletscan.web.preview import LivePreview
from palletscan.web.server import DashboardServer
from palletscan.web.store import ReadStore


def _realtime_cfg(base: Path) -> AppConfig:
    cfg = AppConfig().model_copy(
        update={
            "synthetic": SyntheticConfig(
                width=640,
                height=360,
                fps=30.0,
                seed=77,
                num_passes=2,
                speed_mph_range=(3.0, 5.0),
                angle_deg_range=(0.0, 10.0),
                contrast_range=(0.8, 1.0),
                noise_sigma_range=(1.0, 3.0),
                occlusion_max_frac=0.0,
                idle_s_range=(0.4, 0.6),
                realtime=True,  # paced like a live camera
            ),
        }
    )
    cfg = apply_overrides(cfg, data_dir=base)
    return cfg.model_copy(
        update={
            "sinks": cfg.sinks.model_copy(
                update={"console": ConsoleSinkConfig(enabled=False)}
            )
        }
    )


def test_mjpeg_stream_yields_parts_and_app_survives(tmp_path: Path) -> None:
    cfg = _realtime_cfg(tmp_path)
    web_cfg = WebConfig(preview_fps=20.0, preview_width=320)
    runner = PipelineRunner.from_config(cfg)
    source_id = runner.source.source_id
    preview = LivePreview(source_id, web_cfg)
    runner.preview = preview
    ctx = DashboardContext(
        snapshots={source_id: runner.metrics.snapshot},
        previews={source_id: preview},
        business=None,
        store=ReadStore(cfg.sinks.sqlite.path),
        evidence_root=cfg.evidence.dir,
        web=web_cfg,
    )
    server = DashboardServer(create_app(ctx), host="127.0.0.1", port=0)
    server.start()
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        base = server.url
        # Unknown id 404s while the real stream is available.
        assert httpx.get(f"{base}/live/nope", timeout=10).status_code == 404

        with httpx.stream(
            "GET", f"{base}/live/{source_id}", timeout=30
        ) as resp:
            assert resp.status_code == 200
            assert "multipart/x-mixed-replace" in resp.headers["content-type"]
            buf = b""
            for chunk in resp.iter_bytes():
                buf += chunk
                if buf.count(b"\xff\xd8") >= 2:  # two JPEG SOI markers
                    break
        assert buf.count(b"--palletscanframe") >= 2
        assert b"Content-Type: image/jpeg" in buf

        # The app keeps serving after a streaming client disconnects.
        stats = httpx.get(f"{base}/stats.json", timeout=10)
        assert stats.status_code == 200
        assert source_id in stats.json()["cameras"]
    finally:
        runner.stop()
        thread.join(timeout=30)
        server.stop()
    assert not thread.is_alive()
