"""``palletscan`` command-line interface.

Subcommands: ``run`` (the configured source — live cameras in
production), ``synth`` (the synthetic source), ``replay`` (a recorded
clip), ``calibrate`` (probe/verify/lock camera settings), ``selftest``
(refuse-to-run-blind startup checks), ``version``.

Exit codes: 0 clean; 1 software failure (check logs); 2 usage error;
**3 watchdog escalation** ("USB stack wedged, check cable/hub") — the
supervisor must restart the process on any nonzero exit.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import palletscan
from palletscan.config import AppConfig, apply_overrides, load_config
from palletscan.logging_setup import setup_logging

if TYPE_CHECKING:
    from palletscan.app import PipelineRunner
    from palletscan.station import StationRunner
    from palletscan.web.server import DashboardServer


def _add_synth_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "synth", help="run the pipeline on generated synthetic pallet passes"
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
        "replay", help="replay a recorded .mp4/.avi clip through the pipeline"
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
        "run", help="run the pipeline on the configured source (live cameras)"
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


def _add_selftest_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "selftest", help="startup checks: cameras, full-pipeline decode, disk"
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument(
        "--skip-camera", action="store_true", help="skip the camera checks"
    )
    p.add_argument(
        "--data-dir", type=Path, default=None,
        help="scratch directory for the pipeline-decode check outputs",
    )


def _install_sigint(runner: "PipelineRunner | StationRunner") -> None:
    import signal

    def _on_sigint(*_: object) -> None:
        # First Ctrl-C drains gracefully; restoring the default handler
        # lets a second Ctrl-C force-quit a wedged shutdown.
        runner.stop()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _on_sigint)


def _exit_code_for(exc: BaseException) -> int:
    """Map a pipeline failure to its exit code (3 = watchdog escalation,
    ruling #5: 'USB stack wedged, check cable/hub' vs 'software crashed,
    check logs' — distinguishable at the supervisor without log diving)."""
    from palletscan.reliability.watchdog import WatchdogEscalation

    return 3 if isinstance(exc.__cause__, WatchdogEscalation) else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from palletscan.app import PipelineRunner
    from palletscan.station import StationRunner

    cfg = load_config(args.config)
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
    setup_logging(cfg.logging.level)
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
    _install_sigint(runner)
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


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from palletscan.calibrate import CalibrateOptions, run_calibration

    cfg = load_config(args.config)
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

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level)
    report = run_selftest(
        cfg, skip_camera=args.skip_camera, data_dir=args.data_dir
    )
    print(report.format())
    return 0 if report.ok else 1


def _cmd_synth(args: argparse.Namespace) -> int:
    from palletscan.app import PipelineRunner
    from palletscan.sources.synthetic import SyntheticSource

    cfg = load_config(args.config)
    cfg = apply_overrides(
        cfg, num_passes=args.passes, seed=args.seed, data_dir=args.data_dir
    )
    setup_logging(cfg.logging.level)
    truth_dir = args.data_dir if args.data_dir is not None else Path("data")
    truth_path = truth_dir / "truth.jsonl"
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
        _install_sigint(station)
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
            station_summary = station.run(stats_interval_s=args.stats_interval)
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
    _install_sigint(runner)
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

    cfg = load_config(args.config)
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

    cfg = load_config(args.config)
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
    setup_logging(cfg.logging.level)
    runner = PipelineRunner.from_config(cfg)
    _install_sigint(runner)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="palletscan",
        description="Fixed-camera QR/Data Matrix pallet scanning pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print version")
    _add_run_parser(sub)
    _add_synth_parser(sub)
    _add_replay_parser(sub)
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
