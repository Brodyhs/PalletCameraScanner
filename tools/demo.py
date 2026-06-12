"""The end-to-end demo (spec §10.5): the full system on synthetic input
with the live dashboard open in your browser.

Spawns ``python -m palletscan synth --ab --dashboard`` on
``config/demo.yaml`` (realtime-paced A/B passes), polls ``/stats.json``
until the dashboard serves, opens the browser, then waits on the child and
propagates its exit code. Ctrl-C reaches the foreground process group and
drains gracefully through the pipeline's own handlers.

Run (from the repo root, venv active):

  python tools/demo.py
  python tools/demo.py --no-browser --max-seconds 30   # smoke mode

If port 8000 is taken, the child exits 2 with a clean message — edit
``web.port`` in config/demo.yaml.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "demo.yaml"


def wait_until_ready(
    probe: Callable[[], bool],
    timeout_s: float,
    *,
    child_alive: Callable[[], bool] = lambda: True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    interval_s: float = 0.25,
) -> bool:
    """Poll ``probe`` until truthy; False on timeout or child death."""
    deadline = clock() + timeout_s
    while clock() < deadline:
        if not child_alive():
            return False
        if probe():
            return True
        sleep(interval_s)
    return False


def _stats_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def _stop(child: subprocess.Popen) -> int:
    """Smoke-mode stop. POSIX drains via SIGTERM; Windows smoke runs get a
    hard terminate (CTRL events can't target a same-group child) — the
    interactive Windows path is Ctrl-C, which the child handles itself."""
    import signal

    if child.poll() is None:
        if sys.platform == "win32":
            child.terminate()
        else:
            child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
    return child.wait()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--data-dir", type=Path, default=Path("data/demo"))
    ap.add_argument(
        "--no-browser", action="store_true", help="don't open a browser tab"
    )
    ap.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="stop the demo after N seconds (smoke tests)",
    )
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"demo: could not load {args.config}: {exc}", file=sys.stderr)
        return 2
    if not cfg.web.enabled or cfg.web.port == 0:
        print(
            "demo: the demo config needs web.enabled: true and a fixed "
            "web.port (the readiness poll must know where to look)",
            file=sys.stderr,
        )
        return 2
    url = f"http://{cfg.web.host}:{cfg.web.port}"

    cmd = [
        sys.executable,
        "-m",
        "palletscan",
        "synth",
        "--ab",
        "--dashboard",
        "--config",
        str(args.config),
        "--data-dir",
        str(args.data_dir),
    ]
    child = subprocess.Popen(cmd)
    try:
        ready = wait_until_ready(
            lambda: _stats_ok(url + "/stats.json"),
            timeout_s=60.0,
            child_alive=lambda: child.poll() is None,
        )
        if not ready:
            if child.poll() is not None:
                # The child already printed why (port in use, bad config).
                return child.wait()
            print("demo: dashboard never became ready", file=sys.stderr)
            return _stop(child) or 1
        print(f"dashboard ready at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        if args.max_seconds is not None:
            try:
                return child.wait(timeout=args.max_seconds)
            except subprocess.TimeoutExpired:
                return _stop(child)
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                # Ctrl-C hit the whole foreground group: the child is
                # draining and will print its summary; keep waiting.
                continue
    finally:
        if child.poll() is None:
            _stop(child)


if __name__ == "__main__":
    sys.exit(main())
