"""Record a synthetic scenario to an .avi clip + truth JSONL for replay.

Run:  python tools/record_synthetic.py --out data/clips/synth40.avi --passes 40
Then: palletscan replay data/clips/synth40.avi --speed 0 \
          --truth data/clips/synth40.truth.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import apply_overrides, load_config
from palletscan.logging_setup import setup_logging
from palletscan.sources.record import record_synthetic_clip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help=".avi output path")
    ap.add_argument("--config", type=Path, default=None, help="YAML config path")
    ap.add_argument("--passes", type=int, default=None, help="number of passes")
    ap.add_argument("--seed", type=int, default=None, help="scenario seed")
    ap.add_argument(
        "--truth",
        type=Path,
        default=None,
        help="truth JSONL path (default: <out>.truth.jsonl)",
    )
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    cfg = apply_overrides(cfg, num_passes=args.passes, seed=args.seed)
    setup_logging(cfg.logging.level)
    res = record_synthetic_clip(cfg, args.out, args.truth)
    print(
        f"wrote {res.clip_path} ({res.frames} frames @ {res.fps:g} fps), "
        f"truth {res.truth_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
