"""``palletscan`` command-line interface.

Phase 1 subcommands: ``synth`` (run the full pipeline on the synthetic
source), ``version``. Later phases add ``run``, ``replay``, ``calibrate``,
``selftest`` as further subparsers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import palletscan
from palletscan.config import apply_overrides, load_config
from palletscan.logging_setup import setup_logging


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


def _cmd_synth(args: argparse.Namespace) -> int:
    import signal

    from palletscan.app import PipelineRunner
    from palletscan.sources.synthetic import SyntheticSource

    cfg = load_config(args.config)
    cfg = apply_overrides(
        cfg, num_passes=args.passes, seed=args.seed, data_dir=args.data_dir
    )
    setup_logging(cfg.logging.level)
    runner = PipelineRunner.from_config(cfg)

    def _on_sigint(*_: object) -> None:
        # First Ctrl-C drains gracefully; restoring the default handler
        # lets a second Ctrl-C force-quit a wedged shutdown.
        runner.stop()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _on_sigint)
    summary = runner.run()
    if isinstance(runner.source, SyntheticSource):
        truth_dir = args.data_dir if args.data_dir is not None else Path("data")
        truth_path = truth_dir / "truth.jsonl"
        runner.source.write_truth_jsonl(truth_path)
        print(f"truth written to {truth_path}")
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

    args = parser.parse_args(argv)
    if args.command == "version":
        print(palletscan.__version__)
        return 0
    if args.command == "synth":
        return _cmd_synth(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
