"""``palletscan`` command-line interface.

Subcommands: ``run`` (the configured source — live cameras in
production), ``synth`` (the synthetic source), ``replay`` (a recorded
clip), ``supervise`` (restart-on-any-nonzero-exit wrapper around a writer
command; what the Windows scheduled task runs), ``calibrate``
(probe/verify/lock camera settings), ``selftest`` (refuse-to-run-blind
startup checks), ``version``.

Exit codes: 0 clean; 1 software failure (check logs); 2 usage error;
**3 watchdog escalation** ("USB stack wedged, check cable/hub");
**4 another instance holds the lock** (run/synth/replay are
single-instance per data-dir) — the supervisor must restart the process
on any nonzero exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import palletscan
from palletscan.config import AppConfig, apply_overrides, load_config
from palletscan.logging_setup import setup_logging

if TYPE_CHECKING:
    from palletscan.app import PipelineRunner
    from palletscan.reliability.instance_lock import InstanceLock
    from palletscan.reliability.supervisor import Supervisor
    from palletscan.station import StationRunner
    from palletscan.web.server import DashboardServer


def _add_synth_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "synth",
        allow_abbrev=False,
        help="run the pipeline on generated synthetic pallet passes",
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument("--passes", type=int, default=None, help="number of passes")
    p.add_argument("--seed", type=int, default=None, help="scenario seed")
    p.add_argument(
        "--ab",
        action="store_true",
        help="A/B mode: two same-seed synthetic sources (synthA/synthB) "
        "through per-camera pipelines with cross-camera business dedup",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="rebase events.jsonl / palletscan.db / evidence under this "
        "directory (default: keep the paths from the config file)",
    )
    _add_stats_interval(p)
    _add_dashboard_flag(p)


def _add_stats_interval(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--stats-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="log a structured metrics snapshot line every N seconds",
    )


def _add_dashboard_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dashboard",
        action="store_true",
        help="serve the live dashboard while running (also enabled by "
        "web.enabled in the config); localhost-bound, no auth",
    )


class _DashboardUnavailable(Exception):
    """Dashboard prerequisites missing (configuration error, exit 2)."""


def _start_dashboard(
    cfg: AppConfig,
    runners: "dict[str, PipelineRunner]",
    business: Callable[[], dict[str, Any]] | None,
) -> "DashboardServer":
    """Build previews + context and start the server (before runner.run)."""
    from palletscan.web.app import DashboardContext, create_app
    from palletscan.web.preview import LivePreview
    from palletscan.web.server import DashboardServer
    from palletscan.web.store import ReadStore, ReadStoreError

    from palletscan.web.server import DashboardServerError

    if not cfg.sinks.sqlite.enabled:
        raise _DashboardUnavailable(
            "the dashboard reads events from SQLite; enable sinks.sqlite "
            "in the config to use --dashboard"
        )
    snapshots = {}
    previews = {}
    for source_id, runner in runners.items():
        preview = LivePreview(source_id, cfg.web)
        runner.preview = preview
        previews[source_id] = preview
        snapshots[source_id] = runner.metrics.snapshot
    try:
        store = ReadStore(cfg.sinks.sqlite.path, cfg.report.manifest_path)
    except ReadStoreError as exc:
        # An unopenable events DB is a configuration error: clean message,
        # exit 2 — same treatment as a bad bind below.
        raise _DashboardUnavailable(str(exc)) from exc
    ctx = DashboardContext(
        snapshots=snapshots,
        previews=previews,
        business=business,
        store=store,
        evidence_root=cfg.evidence.dir,
        web=cfg.web,
    )
    server = DashboardServer(create_app(ctx), cfg.web.host, cfg.web.port)
    try:
        server.start()
    except DashboardServerError as exc:
        # Port in use / bad bind: a clean message, not a stack dump, is the
        # operator's first impression of the trial run.
        raise _DashboardUnavailable(str(exc)) from exc
    print(f"dashboard serving on {server.url}")
    return server


def _add_replay_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "replay",
        allow_abbrev=False,
        help="replay a recorded .mp4/.avi clip through the pipeline",
    )
    p.add_argument("file", type=Path, help="video file to replay")
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument(
        "--speed",
        type=float,
        default=None,
        help="playback pacing: 1.0 as-if-live, >1 accelerated, 0 unpaced "
        "(default: config video.speed)",
    )
    p.add_argument(
        "--loop",
        type=int,
        default=None,
        help="play count, 0 = loop forever (default: config video.loop)",
    )
    p.add_argument(
        "--fps-override",
        type=float,
        default=None,
        help="frame rate to assume when the file's metadata is broken",
    )
    p.add_argument(
        "--truth",
        type=Path,
        default=None,
        help="truth JSONL (from tools/record_synthetic.py) to reconcile "
        "decoded payloads against",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="rebase events.jsonl / palletscan.db / evidence under this "
        "directory (default: keep the paths from the config file)",
    )
    _add_stats_interval(p)
    _add_dashboard_flag(p)


def _add_run_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "run",
        allow_abbrev=False,
        help="run the pipeline on the configured source (live cameras)",
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument(
        "--camera",
        type=str,
        default=None,
        help="cameras[].id to run (overrides source.camera)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="rebase events.jsonl / palletscan.db / evidence under this "
        "directory (default: keep the paths from the config file)",
    )
    _add_stats_interval(p)
    _add_dashboard_flag(p)


def _add_dashboard_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "dashboard",
        allow_abbrev=False,
        help="serve the dashboard read-only against an existing events DB "
        "(no runners; how a finished trial gets reviewed)",
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="read events.jsonl / palletscan.db / evidence from this "
        "directory (matches the --data-dir the run used)",
    )


def _add_calibrate_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "calibrate",
        allow_abbrev=False,
        help="probe camera modes, verify controls, lock-and-save settings",
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument("--list", action="store_true", help="list devices and exit")
    p.add_argument("--camera", type=str, default=None, help="cameras[].id")
    p.add_argument(
        "--name", type=str, default=None,
        help="device-name substring (creates a fresh entry; pairs with --camera as its id)",
    )
    p.add_argument("--fourcc", type=str, default=None, help="pin a FOURCC")
    p.add_argument("--width", type=int, default=None, help="pin a width")
    p.add_argument("--height", type=int, default=None, help="pin a height")
    p.add_argument("--fps", type=float, default=None, help="pin a frame rate")
    p.add_argument("--exposure", type=float, default=None, help="raw backend value")
    p.add_argument("--gain", type=float, default=None, help="raw backend value")
    auto = p.add_mutually_exclusive_group()
    auto.add_argument(
        "--auto-exposure", dest="auto_exposure", action="store_true", default=None
    )
    auto.add_argument(
        "--no-auto-exposure", dest="auto_exposure", action="store_false"
    )
    p.add_argument(
        "--seconds", type=int, default=5, help="live metrics loop duration"
    )
    p.add_argument(
        "--save", action="store_true",
        help="upsert the locked entry into the --config file",
    )
    p.add_argument(
        "--preview", action="store_true",
        help="cv2 preview window (main thread only; q quits, s saves)",
    )


def _add_supervise_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "supervise",
        allow_abbrev=False,
        help="restart a writer command on any nonzero exit, with crash-loop "
        "backoff, countable exit codes (logs/restarts.jsonl) and a "
        "stop-file stop channel (what the Windows scheduled task runs)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="directory for the supervisor lock, logs/restarts.jsonl and "
        "the stop-file; appended to the child as --data-dir unless the "
        "child args carry their own",
    )
    p.add_argument(
        "--grace-s",
        type=float,
        default=15.0,
        help="seconds the child gets to drain after the stop signal",
    )
    p.add_argument(
        "--backoff-base-s", type=float, default=5.0, help="restart delay"
    )
    p.add_argument(
        "--backoff-cap-s",
        type=float,
        default=300.0,
        help="crash-loop backoff ceiling",
    )
    p.add_argument(
        "--stable-after-s",
        type=float,
        default=60.0,
        help="a child run at least this long resets the backoff",
    )
    p.add_argument(
        "child",
        nargs=argparse.REMAINDER,
        metavar="-- CHILD ...",
        help="the supervised command after --: run|synth|replay [args]",
    )


def _add_selftest_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "selftest",
        allow_abbrev=False,
        help="startup checks: cameras, full-pipeline decode, disk",
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument(
        "--skip-camera", action="store_true", help="skip the camera checks"
    )
    p.add_argument(
        "--data-dir", type=Path, default=None,
        help="scratch directory for the pipeline-decode check outputs",
    )


def _install_stop_signals(runner: "PipelineRunner | StationRunner") -> None:
    """Graceful-drain handlers for SIGINT, SIGTERM and (Windows) SIGBREAK.

    The first signal asks the runner to stop and drain; restoring the
    default handlers lets a second signal force-quit a wedged shutdown.
    SIGTERM is what the POSIX supervisor sends; SIGBREAK is CTRL_BREAK —
    the only console event deliverable to a Windows child process group
    (see reliability/supervisor.py).
    """
    import signal

    stop_signals = [signal.SIGINT]
    for name in ("SIGTERM", "SIGBREAK"):
        extra = getattr(signal, name, None)
        if extra is not None:
            stop_signals.append(extra)

    def _on_stop(*_: object) -> None:
        runner.stop()
        for sig in stop_signals:
            signal.signal(sig, signal.SIG_DFL)

    for sig in stop_signals:
        signal.signal(sig, _on_stop)


class _WriterLease:
    """The writer commands' process-global acquisitions — the instance
    lock and the rotating file handler — undone together by ``release()``.
    main() runs in-process under pytest, so neither may leak across calls.
    """

    def __init__(
        self, lock: "InstanceLock", handler: logging.Handler | None
    ) -> None:
        self._lock = lock
        self._handler = handler

    def release(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler.close()
            self._handler = None
        self._lock.release()


def _hold_lock_and_file_logging(
    cfg: AppConfig, command: str
) -> "_WriterLease | None":
    """Acquire the per-data-dir single-instance lock, then start rotating
    file logging.

    Lock scope == file-logging scope (D2/D3): rotation's rename must be
    single-writer on Windows. The lock comes before any camera, sink or
    evidence path is touched. Returns None on contention (the message is
    already printed; the caller exits 4).
    """
    from palletscan.logging_setup import add_rotating_file_handler, prune_old_logs
    from palletscan.reliability.instance_lock import InstanceLock, InstanceLockHeld

    lock = InstanceLock(cfg.lock.path)
    try:
        lock.acquire()
    except InstanceLockHeld as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return None
    handler = None
    if cfg.logging.file.enabled:
        prune_old_logs(cfg.logging.file.dir, cfg.logging.file.max_age_days)
        handler = add_rotating_file_handler(cfg.logging.file)
    # One startup line per run: marks the run boundary in the rotating log
    # (and materializes the delay=True file even on quiet runs).
    logging.getLogger(__name__).info(
        "%s started: lock %s held by pid %d", command, cfg.lock.path, os.getpid()
    )
    return _WriterLease(lock, handler)


def _start_parent_watch_if_supervised(
    runner: "PipelineRunner | StationRunner",
) -> "object | None":
    """When spawned by `palletscan supervise` (SUPERVISOR_PID_ENV set),
    watch the supervisor and gracefully self-stop if it dies: an orphaned
    writer holds the instance lock, ignores every stop channel, and keeps
    scanning under a stale config until killed by hand (REVIEW finding 6).
    Returns the watch (caller must ``stop()`` it in its finally — main()
    runs in-process under pytest and must not leak threads), or None."""
    from palletscan.reliability.supervisor import SUPERVISOR_PID_ENV, ParentWatch

    raw = os.environ.get(SUPERVISOR_PID_ENV)
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    watch = ParentWatch(pid, runner.stop)
    watch.start()
    return watch


def _stop_parent_watch(watch: "object | None") -> None:
    if watch is not None:
        watch.stop()  # type: ignore[attr-defined]


def _load_config_checked(path: Path | None, command: str) -> AppConfig | None:
    """Load the YAML config; any load/validation failure becomes the
    documented exit-2 contract (clean message, no traceback) instead of an
    exit-1 raw pydantic dump — the supervisor's dedicated exit-2
    "fix the config" branch depends on it (REVIEW finding b2). Returns
    None after printing; the caller returns 2."""
    try:
        return load_config(path)
    except Exception as exc:
        print(
            f"{command}: invalid config {path}: {exc}\n"
            f"{command}: fix the config file and retry",
            file=sys.stderr,
        )
        return None


def _exit_code_for(exc: BaseException) -> int:
    """Map a pipeline failure to its exit code (3 = watchdog escalation,
    ruling #5: 'USB stack wedged, check cable/hub' vs 'software crashed,
    check logs' — distinguishable at the supervisor without log diving)."""
    from palletscan.reliability.watchdog import WatchdogEscalation

    return 3 if isinstance(exc.__cause__, WatchdogEscalation) else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from palletscan.app import PipelineRunner
    from palletscan.station import StationRunner

    cfg = _load_config_checked(args.config, "run")
    if cfg is None:
        return 2
    cfg = apply_overrides(cfg, data_dir=args.data_dir)
    if args.camera is not None:
        # An explicit --camera narrows an A/B config to one arm.
        cfg = cfg.model_copy(
            update={
                "source": cfg.source.model_copy(
                    update={"camera": args.camera, "cameras": None}
                )
            }
        )
    setup_logging(cfg.logging.level)  # stderr first: lock failures must log
    lease = _hold_lock_and_file_logging(cfg, "run")
    if lease is None:
        return 4
    watch = None
    try:
        try:
            runner: PipelineRunner | StationRunner = (
                StationRunner(cfg)
                if cfg.source.cameras is not None
                else PipelineRunner.from_config(cfg)
            )
        except Exception as exc:
            # Fail-fast construction (refuse to run blind): bad selector,
            # missing device, capture that will not open.
            print(f"run: {exc}", file=sys.stderr)
            return 1
        _install_stop_signals(runner)
        watch = _start_parent_watch_if_supervised(runner)
        dashboard = None
        if args.dashboard or cfg.web.enabled:
            if isinstance(runner, StationRunner):
                runners, business = runner.runners, runner.deduper.stats
            else:
                runners, business = {runner.source.source_id: runner}, None
            try:
                dashboard = _start_dashboard(cfg, runners, business)
            except _DashboardUnavailable as exc:
                print(f"run: {exc}", file=sys.stderr)
                return 2
        try:
            summary = runner.run(stats_interval_s=args.stats_interval)
        except RuntimeError as exc:
            print(f"run: {exc.__cause__ or exc}", file=sys.stderr)
            return _exit_code_for(exc)
        finally:
            if dashboard is not None:
                dashboard.stop()
        print(summary.format())
        return 0
    finally:
        _stop_parent_watch(watch)
        lease.release()


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from palletscan.calibrate import CalibrateOptions, run_calibration

    cfg = _load_config_checked(args.config, "calibrate")
    if cfg is None:
        return 2
    setup_logging(cfg.logging.level)
    opts = CalibrateOptions(
        list_only=args.list,
        camera=args.camera,
        name=args.name,
        fourcc=args.fourcc,
        width=args.width,
        height=args.height,
        fps=args.fps,
        exposure=args.exposure,
        gain=args.gain,
        auto_exposure=args.auto_exposure,
        seconds=args.seconds,
        save=args.save,
        config_path=args.config,
        preview=args.preview,
    )
    return run_calibration(cfg, opts)


def _cmd_selftest(args: argparse.Namespace) -> int:
    from palletscan.selftest import run_selftest

    cfg = _load_config_checked(args.config, "selftest")
    if cfg is None:
        return 2
    # Rebase exactly like _cmd_run does: the disk gate must probe the
    # volumes the deployed station actually writes to, not the config
    # file's cwd-relative defaults (REVIEW finding 12 — the only disk
    # check in the system was green while the real volume filled).
    cfg = apply_overrides(cfg, data_dir=args.data_dir)
    setup_logging(cfg.logging.level)
    report = run_selftest(
        cfg, skip_camera=args.skip_camera, data_dir=args.data_dir
    )
    print(report.format())
    return 0 if report.ok else 1


def _cmd_synth(args: argparse.Namespace) -> int:
    from palletscan.app import PipelineRunner
    from palletscan.sources.synthetic import SyntheticSource

    cfg = _load_config_checked(args.config, "synth")
    if cfg is None:
        return 2
    # Pin the synthetic source no matter what the config declares (the
    # same pin replay applies to video): `synth` on a camera config must
    # never silently open the real cameras and write real passes into
    # production sinks under the synth banner (REVIEW finding b13).
    cfg = cfg.model_copy(
        update={
            "source": cfg.source.model_copy(
                update={"type": "synthetic", "camera": None, "cameras": None}
            )
        }
    )
    cfg = apply_overrides(
        cfg, num_passes=args.passes, seed=args.seed, data_dir=args.data_dir
    )
    setup_logging(cfg.logging.level)  # stderr first: lock failures must log
    truth_dir = args.data_dir if args.data_dir is not None else Path("data")
    truth_path = truth_dir / "truth.jsonl"
    lease = _hold_lock_and_file_logging(cfg, "synth")
    if lease is None:
        return 4
    watch = None
    try:
        if args.ab:
            from palletscan.sources.factory import synthetic_tail_s
            from palletscan.station import StationRunner

            # Same-seed sources produce bit-identical pass schedules: two
            # "cameras" on one zone, exercising the full cross-camera merge
            # path without hardware.
            tail = synthetic_tail_s(cfg)
            sources = [
                SyntheticSource(cfg.synthetic, source_id=source_id, tail_s=tail)
                for source_id in ("synthA", "synthB")
            ]
            station = StationRunner(cfg, sources=sources)
            _install_stop_signals(station)
            watch = _start_parent_watch_if_supervised(station)
            dashboard = None
            if args.dashboard or cfg.web.enabled:
                try:
                    dashboard = _start_dashboard(
                        cfg, station.runners, station.deduper.stats
                    )
                except _DashboardUnavailable as exc:
                    print(f"synth: {exc}", file=sys.stderr)
                    return 2
            try:
                station_summary = station.run(
                    stats_interval_s=args.stats_interval
                )
            except RuntimeError as exc:
                # Same contract as _cmd_run: station.py chains the runner
                # failure's cause precisely so it survives to this mapping —
                # a clean message + exit code (3 = watchdog escalation), not
                # a raw traceback.
                print(f"synth: {exc.__cause__ or exc}", file=sys.stderr)
                return _exit_code_for(exc)
            finally:
                if dashboard is not None:
                    dashboard.stop()
            sources[0].write_truth_jsonl(truth_path)
            print(f"truth written to {truth_path}")
            print(station_summary.format())
            return 0 if station_summary.unaccounted == 0 else 1
        runner = PipelineRunner.from_config(cfg)
        _install_stop_signals(runner)
        watch = _start_parent_watch_if_supervised(runner)
        dashboard = None
        if args.dashboard or cfg.web.enabled:
            try:
                dashboard = _start_dashboard(
                    cfg, {runner.source.source_id: runner}, None
                )
            except _DashboardUnavailable as exc:
                print(f"synth: {exc}", file=sys.stderr)
                return 2
        try:
            summary = runner.run(stats_interval_s=args.stats_interval)
        finally:
            if dashboard is not None:
                dashboard.stop()
        if isinstance(runner.source, SyntheticSource):
            runner.source.write_truth_jsonl(truth_path)
            print(f"truth written to {truth_path}")
        print(summary.format())
        return 0 if summary.unaccounted == 0 else 1
    finally:
        _stop_parent_watch(watch)
        lease.release()


def _install_supervise_signals(sup: "Supervisor") -> None:
    """Stop the supervisor (and its child) gracefully on console signals.

    ``forward`` decides whether the supervisor signals the child: a POSIX
    terminal Ctrl-C already hit the whole foreground group (the child is
    draining; signalling again would trip its second-signal-forces path),
    while a directed SIGTERM — or any Windows ctrl event, since the child
    lives in its own process group — reached the supervisor alone.
    """
    import signal

    def _on_sigint(*_: object) -> None:
        sup.request_stop(forward=sys.platform == "win32")

    def _on_directed(*_: object) -> None:
        sup.request_stop(forward=True)

    signal.signal(signal.SIGINT, _on_sigint)
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _on_directed)


def _child_carries_data_dir(child: list[str]) -> bool:
    """True when the child args set their own data dir, in either argparse
    spelling (``--data-dir X`` or ``--data-dir=X``). The ``=`` form used to
    slip past an exact-token check, get the supervisor's own ``--data-dir``
    appended after it, and argparse-last-wins silently ran the child on the
    supervisor's directory (REVIEW finding b1). Abbreviations like
    ``--data`` are no longer a spelling: the CLI parses with
    ``allow_abbrev=False``, so they fail loudly in the child instead."""
    return any(
        tok == "--data-dir" or tok.startswith("--data-dir=") for tok in child
    )


def _cmd_supervise(args: argparse.Namespace) -> int:
    from palletscan.config import LogFileConfig
    from palletscan.logging_setup import add_rotating_file_handler
    from palletscan.reliability.instance_lock import InstanceLock, InstanceLockHeld
    from palletscan.reliability.supervisor import Supervisor, SupervisorOptions

    child = list(args.child)
    if child[:1] == ["--"]:
        child = child[1:]
    if not child or child[0] not in ("run", "synth", "replay"):
        print(
            "supervise: the child command (after --) must start with run, "
            "synth or replay",
            file=sys.stderr,
        )
        return 2
    if not _child_carries_data_dir(child):
        # The diagram in the RUNBOOK holds by construction: the child's
        # lock/sinks/logs land under the supervisor's data-dir.
        child += ["--data-dir", str(args.data_dir)]
    setup_logging("INFO")
    lock = InstanceLock(args.data_dir / "palletscan.supervisor.lock")
    try:
        lock.acquire()
    except InstanceLockHeld as exc:
        print(f"supervise: {exc}", file=sys.stderr)
        return 4
    # The supervisor's own rotating file — never the child's, so rollover
    # renames stay single-writer on both sides.
    handler = add_rotating_file_handler(
        LogFileConfig(dir=args.data_dir / "logs"), filename="supervisor.jsonl"
    )
    try:
        sup = Supervisor(
            SupervisorOptions(
                data_dir=args.data_dir,
                command=[sys.executable, "-m", "palletscan", *child],
                grace_s=args.grace_s,
                backoff_base_s=args.backoff_base_s,
                backoff_cap_s=args.backoff_cap_s,
                stable_after_s=args.stable_after_s,
            )
        )
        _install_supervise_signals(sup)
        return sup.run()
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
        lock.release()


def _wait_for_interrupt() -> None:
    """Block until Ctrl-C (patchable seam for tests)."""
    import time

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from palletscan.web.app import DashboardContext, create_app
    from palletscan.web.server import DashboardServer, DashboardServerError
    from palletscan.web.store import ReadStore, ReadStoreError

    cfg = _load_config_checked(args.config, "dashboard")
    if cfg is None:
        return 2
    cfg = apply_overrides(cfg, data_dir=args.data_dir)
    setup_logging(cfg.logging.level)
    if not cfg.sinks.sqlite.enabled:
        print(
            "dashboard: sinks.sqlite is disabled in this config; there is "
            "no events DB to serve",
            file=sys.stderr,
        )
        return 2
    db = cfg.sinks.sqlite.path
    if not db.is_file():
        # Refuse to invent an empty DB: a typo'd path silently showing
        # "no events" would misreport a finished trial.
        print(
            f"dashboard: events DB not found at {db}; pass the same "
            "--config/--data-dir the run used",
            file=sys.stderr,
        )
        return 2
    try:
        store = ReadStore(db, cfg.report.manifest_path)
    except ReadStoreError as exc:
        # e.g. a readonly DB file: the is_file() check passes but the web
        # tables cannot be prepared — clean message, not a raw traceback.
        print(f"dashboard: {exc}", file=sys.stderr)
        return 2
    ctx = DashboardContext(
        snapshots={},
        previews={},
        business=None,
        store=store,
        evidence_root=cfg.evidence.dir,
        web=cfg.web,
    )
    server = DashboardServer(create_app(ctx), cfg.web.host, cfg.web.port)
    try:
        server.start()
    except DashboardServerError as exc:
        print(f"dashboard: {exc}", file=sys.stderr)
        return 2
    print(f"dashboard serving on {server.url} (read-only; Ctrl-C to stop)")
    try:
        _wait_for_interrupt()
    finally:
        server.stop()
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from palletscan.app import PipelineRunner, reconcile_truth
    from palletscan.config import VideoConfig
    from palletscan.sources.synthetic import load_truth_jsonl

    cfg = _load_config_checked(args.config, "replay")
    if cfg is None:
        return 2
    cfg = apply_overrides(cfg, data_dir=args.data_dir)
    video_update: dict = {"path": args.file}
    if args.speed is not None:
        video_update["speed"] = args.speed
    if args.loop is not None:
        video_update["loop"] = args.loop
    if args.fps_override is not None:
        video_update["fps_override"] = args.fps_override
    try:
        # Full re-validation: model_copy(update=) would skip the field
        # validators, letting a typo'd --speed/--loop/--fps-override
        # corrupt every source-clock timestamp downstream.
        video_cfg = VideoConfig(**{**cfg.video.model_dump(), **video_update})
    except ValidationError as exc:
        print(f"invalid replay options:\n{exc}", file=sys.stderr)
        return 2
    if args.truth is not None and video_cfg.loop != 1:
        print(
            "--truth requires a single play (--loop 1): reconciliation "
            "matches first-play timestamps, so later loops would go "
            "unverified and mask silent drops",
            file=sys.stderr,
        )
        return 2
    cfg = cfg.model_copy(
        update={
            "source": cfg.source.model_copy(update={"type": "video"}),
            "video": video_cfg,
        }
    )
    setup_logging(cfg.logging.level)  # stderr first: lock failures must log
    lease = _hold_lock_and_file_logging(cfg, "replay")
    if lease is None:
        return 4
    watch = None
    try:
        runner = PipelineRunner.from_config(cfg)
        _install_stop_signals(runner)
        watch = _start_parent_watch_if_supervised(runner)
        dashboard = None
        if args.dashboard or cfg.web.enabled:
            try:
                dashboard = _start_dashboard(
                    cfg, {runner.source.source_id: runner}, None
                )
            except _DashboardUnavailable as exc:
                print(f"replay: {exc}", file=sys.stderr)
                return 2
        try:
            summary = runner.run(stats_interval_s=args.stats_interval)
        finally:
            if dashboard is not None:
                dashboard.stop()
        if args.truth is not None:
            fps = runner.source.nominal_fps or 30.0
            summary.reconciliation = reconcile_truth(
                load_truth_jsonl(args.truth), runner.collected_events, fps
            )
        print(summary.format())
        return 0 if summary.unaccounted == 0 else 1
    finally:
        _stop_parent_watch(watch)
        lease.release()


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (cp1252) and so does the
    # locale encoding used when stdout is piped; the formatted reports use
    # non-ASCII glyphs (box rules etc.), which raises UnicodeEncodeError and
    # crashes selftest/synth/run output on the factory PC. Force UTF-8 up front
    # (no-op where stdout is already UTF-8, e.g. macOS/Linux; guarded so a
    # replaced/captured stream without reconfigure() is simply skipped).
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    # No option abbreviations anywhere: an abbreviated --data-dir spelling
    # ("--data") used to slip past supervise's append gate and silently run
    # the child on the wrong data dir (REVIEW finding b1).
    parser = argparse.ArgumentParser(
        prog="palletscan",
        description="Fixed-camera QR/Data Matrix pallet scanning pipeline",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print version")
    _add_run_parser(sub)
    _add_synth_parser(sub)
    _add_replay_parser(sub)
    _add_supervise_parser(sub)
    _add_dashboard_parser(sub)
    _add_calibrate_parser(sub)
    _add_selftest_parser(sub)

    args = parser.parse_args(argv)
    if args.command == "version":
        print(palletscan.__version__)
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "synth":
        return _cmd_synth(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "supervise":
        return _cmd_supervise(args)
    if args.command == "dashboard":
        return _cmd_dashboard(args)
    if args.command == "calibrate":
        return _cmd_calibrate(args)
    if args.command == "selftest":
        return _cmd_selftest(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
