"""``palletscan`` command-line interface.

Subcommands: ``synth`` (run the full pipeline on the synthetic source),
``replay`` (run a recorded clip through the pipeline), ``version``. Later
phases add ``run``, ``calibrate``, ``selftest`` as further subparsers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import palletscan
from palletscan.config import apply_overrides, load_config
from palletscan.logging_setup import setup_logging

if TYPE_CHECKING:
    from palletscan.app import PipelineRunner


def _add_synth_parser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "synth", help="run the pipeline on generated synthetic pallet passes"
    )
    p.add_argument("--config", type=Path, default=None, help="YAML config path")
    p.add_argument("--passes", type=int, default=None, help="number of passes")
    p.add_argument("--seed", type=int, default=None, help="scenario seed")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="rebase events.jsonl / palletscan.db / evidence under this "
        "directory (default: keep the paths from the config file)",
    )
    _add_stats_interval(p)


def _add_stats_interval(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--stats-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="log a structured metrics snapshot line every N seconds",
    )


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


def _install_sigint(runner: "PipelineRunner") -> None:
    import signal

    def _on_sigint(*_: object) -> None:
        # First Ctrl-C drains gracefully; restoring the default handler
        # lets a second Ctrl-C force-quit a wedged shutdown.
        runner.stop()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _on_sigint)


def _cmd_synth(args: argparse.Namespace) -> int:
    from palletscan.app import PipelineRunner
    from palletscan.sources.synthetic import SyntheticSource

    cfg = load_config(args.config)
    cfg = apply_overrides(
        cfg, num_passes=args.passes, seed=args.seed, data_dir=args.data_dir
    )
    setup_logging(cfg.logging.level)
    runner = PipelineRunner.from_config(cfg)
    _install_sigint(runner)
    summary = runner.run(stats_interval_s=args.stats_interval)
    if isinstance(runner.source, SyntheticSource):
        truth_dir = args.data_dir if args.data_dir is not None else Path("data")
        truth_path = truth_dir / "truth.jsonl"
        runner.source.write_truth_jsonl(truth_path)
        print(f"truth written to {truth_path}")
    print(summary.format())
    return 0 if summary.unaccounted == 0 else 1


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
    summary = runner.run(stats_interval_s=args.stats_interval)
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
    _add_synth_parser(sub)
    _add_replay_parser(sub)

    args = parser.parse_args(argv)
    if args.command == "version":
        print(palletscan.__version__)
        return 0
    if args.command == "synth":
        return _cmd_synth(args)
    if args.command == "replay":
        return _cmd_replay(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
