"""The end-to-end demo (spec §10.5): the full system on synthetic input
with the live dashboard open in your browser.

Spawns ``python -m palletscan synth --ab --dashboard`` on
``config/demo.yaml`` (realtime-paced A/B passes), polls ``/stats.json``
until the dashboard serves, opens the browser, then waits on the child and
propagates its exit code. On POSIX a Ctrl-C reaches the whole foreground
process group and the child drains through the pipeline's own handlers; on
Windows the child's ``CREATE_NEW_PROCESS_GROUP`` suppresses console Ctrl-C
delivery to it, so the parent forwards the stop explicitly (stop-file
latch + CTRL_BREAK).

Run (from the repo root, venv active):

  python tools/demo.py
  python tools/demo.py --no-browser --max-seconds 30   # smoke mode

If port 8000 is taken, the child exits 2 with a clean message — edit
``web.port`` in config/demo.yaml.
"""

from __future__ import annotations

import argparse
import socket
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


def port_in_use(host: str, port: int) -> bool:
    """True when something is already bound on (host, port).

    The readiness poll accepts any HTTP 200, so a pre-existing server on
    the demo port (the LIVE station dashboard, if the demo config ever
    points at its port) would make the demo declare "ready" against real
    trial data while the demo child dies of a port-bind exit 2 underneath
    (REVIEW finding b5). Refuse up front instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def _stop(
    child: subprocess.Popen,
    *,
    stop_file: Path | None = None,
    already_signaled: bool = False,
) -> int:
    """Graceful stop on both platforms. The primary channel is the
    writer-level stop latch (``palletscan.stop`` in the child's data dir,
    watched by reliability/supervisor.py's ``StopFileWatch``) — it needs no
    console, so it works under pytest/CI capture where console ctrl events
    cannot be delivered. POSIX additionally sends SIGTERM and Windows
    CTRL_BREAK_EVENT (the child is spawned with ``CREATE_NEW_PROCESS_GROUP``,
    supervisor.py's pattern) so an interactive stop drains promptly.

    ``already_signaled``: a POSIX Ctrl-C already hit the whole foreground
    group, the child is draining, and its first-signal handler restored
    SIG_DFL — a second signal here would hard-kill it mid-drain (REVIEW
    finding b6). Just wait it out (then kill only a truly wedged child).
    """
    import signal

    if child.poll() is None:
        if not already_signaled:
            if stop_file is not None:
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.touch()
            if sys.platform == "win32":
                # Console-dependent accelerator only: harmless OSError when
                # no console is shared — the latch above still drains it.
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass
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
    if port_in_use(cfg.web.host, cfg.web.port):
        print(
            f"demo: something is already serving on "
            f"{cfg.web.host}:{cfg.web.port} (the live station dashboard?); "
            "refusing to demo against it — edit web.port in the demo config",
            file=sys.stderr,
        )
        return 2

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
    # The child's writer-level stop latch (data-dir scoped, sticky): clear a
    # stale one from a previous demo run so this run doesn't insta-drain.
    stop_file = args.data_dir / "palletscan.stop"
    stop_file.unlink(missing_ok=True)
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        # New process group so _stop's CTRL_BREAK_EVENT targets only the
        # child (supervisor.py's documented pattern); the child still shares
        # the console, which is what lets the break event reach it at all.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    child = subprocess.Popen(cmd, **popen_kwargs)
    interrupted = False
    try:
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
                return _stop(child, stop_file=stop_file) or 1
            print(f"dashboard ready at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            if args.max_seconds is not None:
                try:
                    return child.wait(timeout=args.max_seconds)
                except subprocess.TimeoutExpired:
                    return _stop(child, stop_file=stop_file)
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    if sys.platform == "win32":
                        # CREATE_NEW_PROCESS_GROUP implicitly disables
                        # console Ctrl-C for the child (CreateProcess's
                        # SetConsoleCtrlHandler(NULL, TRUE)), so the event
                        # never reached it — waiting it out would spin
                        # until the synth plan completes. Stop it
                        # explicitly (stop-file latch + CTRL_BREAK).
                        return _stop(child, stop_file=stop_file)
                    # POSIX: Ctrl-C hit the whole foreground group: the
                    # child is draining and will print its summary; keep
                    # waiting.
                    continue
        except KeyboardInterrupt:
            # Ctrl-C outside the wait loop (readiness poll, browser open).
            if sys.platform == "win32":
                # Same as above: the new-process-group child never saw the
                # console event, so it must be stopped explicitly.
                return _stop(child, stop_file=stop_file)
            # POSIX: the console event still reached the child, whose
            # first-signal handler already restored SIG_DFL — sending a
            # second signal would hard-kill it mid-drain (REVIEW finding
            # b6). Wait it out.
            interrupted = True
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    continue
    finally:
        if child.poll() is None:
            _stop(child, stop_file=stop_file, already_signaled=interrupted)


if __name__ == "__main__":
    sys.exit(main())
