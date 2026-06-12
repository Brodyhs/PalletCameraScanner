"""DashboardServer: uvicorn on a background thread with a clean lifecycle.

uvicorn off the main thread skips signal-handler installation (it checks for
the main thread itself), so the existing SIGINT/SIGTERM handling around the
pipeline stays in charge; ``stop()`` sets ``should_exit`` and joins. Our
logging setup is kept — uvicorn runs at ``warning`` so the structured
pipeline logs stay the primary record.
"""

from __future__ import annotations

import logging
import threading
import time

import uvicorn
from fastapi import FastAPI

log = logging.getLogger(__name__)


class DashboardServerError(RuntimeError):
    """The server failed to come up (port in use, bad bind address...)."""


class DashboardServer:
    """Run a FastAPI app on a daemon thread; ``port=0`` picks a free port."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        self._config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            # Without this, graceful shutdown waits FOREVER for in-flight
            # responses — and /live MJPEG streams only complete when the
            # client disconnects, so stop() with a dashboard tab open would
            # stall its full join timeout and leak a still-serving thread.
            timeout_graceful_shutdown=3,
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None
        self._stopped = False

    def _run(self) -> None:
        try:
            self._server.run()
        except SystemExit as exc:  # uvicorn sys.exit()s on bind failure
            log.error("dashboard server exited with code %s", exc.code)

    @property
    def port(self) -> int:
        """The actually-bound port (meaningful once started)."""
        servers = getattr(self._server, "servers", None)
        if servers:
            for srv in servers:
                for sock in srv.sockets or []:
                    return int(sock.getsockname()[1])
        return self._config.port

    @property
    def url(self) -> str:
        return f"http://{self._config.host}:{self.port}"

    def start(self, timeout_s: float = 15.0) -> None:
        """Spawn the server thread and wait until it accepts connections."""
        if self._stopped:
            # stop() consumes the single-use uvicorn.Server (should_exit
            # stays set, its started flag never resets), so a relaunch
            # would report success and serve nothing. Fail loudly until
            # real restart machinery exists — a Phase 5 concern, where
            # service-restart support is being built (ASSUMPTIONS #50).
            raise DashboardServerError(
                "DashboardServer is single-use: construct a new instance "
                "instead of restarting a stopped one"
            )
        if self._thread is not None:
            raise DashboardServerError("server already started")
        self._thread = threading.Thread(
            target=self._run, name="dashboard", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._server.started:
            if not self._thread.is_alive():
                raise DashboardServerError(
                    f"dashboard server exited during startup "
                    f"(bind {self._config.host}:{self._config.port} failed? "
                    "check the log)"
                )
            if time.monotonic() > deadline:
                raise DashboardServerError(
                    f"dashboard server did not start within {timeout_s:.0f}s"
                )
            time.sleep(0.02)
        log.info("dashboard serving on %s", self.url)

    def stop(self, timeout_s: float = 10.0) -> None:
        """Signal shutdown and join the thread. Idempotent. Consumes the
        server: a later start() raises (single-use, see start())."""
        self._stopped = True
        if self._thread is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():  # pragma: no cover - defensive
            log.error("dashboard thread did not stop within %.0fs", timeout_s)
        else:
            self._thread = None
