"""Offline replay + re-score of recorded motion-segment bursts.

Reads a recording directory produced by :class:`SegmentRecorder`
(``recording.enabled``) — ``<dir>/<day>/<candidate>/frame_*.jpg`` +
``meta.json`` — and re-runs the decode cascade over each burst under one or
more decode configurations, WITHOUT touching the live system. The
first-class comparison is **legacy vs zxing** over the same real frames.

For each variant it reports per-decoder attribution (e.g. ``zxing``,
``pyzbar+clahe``), the re-scored decode rate, wall time, recovery candidates
(live misses this config now decodes — hypothetical), and bursts not
reproduced (live passes this config did not re-decode).

FIDELITY CAVEAT: replay re-decodes recorded frames with ONE stored ROI (or
full-frame), coarser than the live per-frame motion ROI, so absolute decode
counts under-count the live run and "not reproduced" is usually an ROI
artifact, not a config failure. The load-bearing signal is the DELTA between
variants over identical frames (e.g. a burst zxing reads that legacy does
not) — reported under "Variant delta".

INTEGRITY RULE (load-bearing): this tool is READ-ONLY over the recording
dir. It emits no events, touches no sink / DB / JSONL, and writes only its
own ``--out`` report. A replay "recovery" over a burst the live system
recorded as a miss is a CANDIDATE for offline analysis, never a confirmed
read — it never folds into the live read rate. The recording's ``payloads``
are ground truth only for bursts the live system decoded (``outcome:
pass``); a ``miss`` carries ``payloads: []`` because there was no live read.

Usage:
    python tools/replay_bursts.py data/recordings
    python tools/replay_bursts.py data/recordings --config a.yaml --config b.yaml
    python tools/replay_bursts.py data/recordings --out replay.md
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import AppConfig, DecodeConfig, DecodeEngineKind, load_config
from palletscan.pipeline.decode_engine import DecodeEngine, PassDecodeContext
from palletscan.types import Frame, Roi

log = logging.getLogger("replay_bursts")


@dataclass(frozen=True, slots=True)
class Burst:
    """One recorded segment on disk: its meta plus the ordered frame files."""

    directory: Path
    meta: dict
    frame_paths: list[Path]

    @property
    def outcome(self) -> str:
        return str(self.meta.get("outcome", "miss"))

    @property
    def payloads(self) -> list[str]:
        return list(self.meta.get("payloads") or [])

    @property
    def roi(self) -> Roi | None:
        r = self.meta.get("roi")
        if isinstance(r, list) and len(r) == 4:
            return Roi(int(r[0]), int(r[1]), int(r[2]), int(r[3]))
        return None


@dataclass(slots=True)
class BurstResult:
    candidate_id: str
    recorded_outcome: str
    ground_truth: list[str]
    decoded_payload: str | None
    decoder: str | None
    frames_to_decode: int | None  # 1-indexed frame at which it first decoded

    @property
    def recovered(self) -> bool:
        """A live miss this config now decodes — hypothetical."""
        return self.recorded_outcome == "miss" and self.decoded_payload is not None

    @property
    def regression(self) -> bool:
        """A live pass whose ground-truth payload this config no longer reads."""
        if self.recorded_outcome != "pass":
            return False
        return self.decoded_payload not in self.ground_truth


@dataclass(slots=True)
class VariantReport:
    name: str
    engine: str
    results: list[BurstResult] = field(default_factory=list)
    wall_s: float = 0.0

    @property
    def recovered(self) -> list[BurstResult]:
        return [r for r in self.results if r.recovered]

    @property
    def regressions(self) -> list[BurstResult]:
        return [r for r in self.results if r.regression]

    @property
    def decoded(self) -> int:
        return sum(1 for r in self.results if r.decoded_payload is not None)

    @property
    def read_rate(self) -> float | None:
        return self.decoded / len(self.results) if self.results else None

    @property
    def attribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            if r.decoder is not None:
                counts[r.decoder] = counts.get(r.decoder, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def find_bursts(recording_dir: Path) -> list[Burst]:
    """Every burst under a recording dir, ordered by candidate id.

    Read-only: globs ``meta.json`` files and their sibling frames; a burst
    with a corrupt/absent meta is skipped with a warning, never fatal.
    """
    bursts: list[Burst] = []
    for meta_path in sorted(recording_dir.rglob("meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping unreadable burst meta %s: %s", meta_path, exc)
            continue
        frames = sorted(meta_path.parent.glob("frame_*.jpg"))
        if not frames:
            log.warning("burst %s has no frames; skipping", meta_path.parent)
            continue
        bursts.append(Burst(meta_path.parent, meta, frames))
    return bursts


def load_burst_frames(burst: Burst) -> list[Frame]:
    """Load a burst's JPEGs back to grayscale Frames (inverse of the write
    path's ``cv2.imwrite``). Frame ts is synthetic — replay scores content,
    not timing — and unreadable frames are dropped, not fatal."""
    source_id = str(burst.meta.get("source_id", "replay"))
    frames: list[Frame] = []
    for path in burst.frame_paths:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            log.warning("unreadable frame %s; skipping", path)
            continue
        try:
            idx = int(path.stem.split("_")[-1])
        except ValueError:
            idx = len(frames)
        frames.append(
            Frame(image=img, ts=idx / 30.0, frame_index=idx, source_id=source_id)
        )
    return frames


def replay_burst(
    engine: DecodeEngine, burst: Burst, confirmations: int
) -> BurstResult:
    """Re-run the cascade over one burst with an advancing decode context.

    Mirrors the live pass: a single :class:`PassDecodeContext` threads
    through the frames so ``frames_attempted`` accrues and the step-3 variant
    fan-out engages exactly as it would live; ``confirmed`` flips only on a
    genuine decode (never pre-set), so a stubborn burst gets the full
    fallback treatment before being called a non-read.
    """
    frames = load_burst_frames(burst)
    ctx = PassDecodeContext()
    counts: dict[str, int] = {}
    for i, frame in enumerate(frames):
        roi = burst.roi or Roi(0, 0, frame.image.shape[1], frame.image.shape[0])
        for res in engine.decode_frame(frame, roi, ctx):
            counts[res.payload] = counts.get(res.payload, 0) + 1
            if counts[res.payload] >= confirmations:
                return BurstResult(
                    candidate_id=str(burst.meta.get("candidate_id", burst.directory.name)),
                    recorded_outcome=burst.outcome,
                    ground_truth=burst.payloads,
                    decoded_payload=res.payload,
                    decoder=res.decoder,
                    frames_to_decode=i + 1,
                )
        # Once a payload is seen (but not yet confirmed) we still let the
        # loop continue; confirmation above is the only exit.
        if counts:
            ctx.confirmed = True
    return BurstResult(
        candidate_id=str(burst.meta.get("candidate_id", burst.directory.name)),
        recorded_outcome=burst.outcome,
        ground_truth=burst.payloads,
        decoded_payload=None,
        decoder=None,
        frames_to_decode=None,
    )


def score_variant(
    name: str, decode_cfg: DecodeConfig, bursts: list[Burst]
) -> VariantReport:
    """Score every burst under one decode config on its own thread pool."""
    report = VariantReport(name=name, engine=decode_cfg.engine.value)
    started = time.perf_counter()
    workers = max(1, decode_cfg.workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        engine = DecodeEngine(decode_cfg, executor)
        for burst in bursts:
            report.results.append(
                replay_burst(engine, burst, max(1, decode_cfg.confirmations))
            )
    report.wall_s = time.perf_counter() - started
    return report


def _variants(args: argparse.Namespace) -> list[tuple[str, DecodeConfig]]:
    """Resolve the decode configs to replay under.

    ``--config`` (repeatable) names full AppConfig YAMLs (variant = file
    stem). With none given, the first-class comparison: legacy vs zxing over
    a base config (``--base`` or defaults), zxing skipped if unavailable.
    """
    if args.config:
        out = []
        for path in args.config:
            cfg = load_config(path)
            out.append((Path(path).stem, cfg.decode))
        return out
    base = load_config(args.base) if args.base else AppConfig()
    variants = [
        ("legacy", base.decode.model_copy(update={"engine": DecodeEngineKind.LEGACY}))
    ]
    try:
        import zxingcpp  # noqa: F401

        variants.append(
            ("zxing", base.decode.model_copy(update={"engine": DecodeEngineKind.ZXING}))
        )
    except ImportError:
        log.warning("zxing-cpp not installed; replaying legacy only "
                    "(pip install -e \".[zxing]\" for the comparison)")
    return variants


_BANNER = (
    "NOTE: replay recoveries are HYPOTHETICAL offline candidates over the "
    "recorded frames — they never fold into the live read rate. A burst "
    "recorded as a miss was a genuine live no-read; a replay decode of it is "
    "a lead to investigate, not a confirmed read.\n"
    "FIDELITY: replay re-decodes the recorded frames using ONE stored ROI "
    "(or full-frame), which is COARSER than the live per-frame motion ROI — "
    "so absolute replay decode counts UNDER-count live, and a burst 'not "
    "reproduced' below is usually a replay-ROI artifact, not a config "
    "failure. The load-bearing signal is the DELTA BETWEEN VARIANTS over the "
    "identical recorded frames (e.g. a burst zxing reads that legacy does "
    "not), not any variant's absolute count vs the live run."
)


def _variant_delta(reports: list[VariantReport]) -> list[str]:
    """The load-bearing signal: bursts decoded by some variants but not
    others, over identical recorded frames (config quality, not ROI
    fidelity). Empty when every variant agrees burst-for-burst."""
    if len(reports) < 2:
        return []
    by_cid: dict[str, dict[str, str | None]] = {}
    for rep in reports:
        for br in rep.results:
            by_cid.setdefault(br.candidate_id, {})[rep.name] = br.decoded_payload
    disagree = [
        cid
        for cid, per in by_cid.items()
        if len({p is not None for p in per.values()}) > 1
    ]
    if not disagree:
        return ["", "Variant agreement: every burst decoded the same across "
                "variants (no config-quality delta)."]
    out = ["", "## Variant delta (the reliable signal)", "",
           "Bursts where variants DISAGREE on decode (identical frames, so "
           "this is config quality, not replay-ROI fidelity):"]
    names = [r.name for r in reports]
    for cid in sorted(disagree):
        per = by_cid[cid]
        cells = ", ".join(
            f"{n}={'read' if per.get(n) else 'no'}" for n in names
        )
        out.append(f"  - {cid}: {cells}")
    return out


def render_report(reports: list[VariantReport], total_bursts: int) -> str:
    lines = [
        "# Burst replay report",
        "",
        _BANNER,
        "",
        f"Bursts replayed: {total_bursts}",
        "",
        "| variant | engine | decoded | replay rate | recovery cand. "
        "| not-reproduced | wall s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        rate = "—" if r.read_rate is None else f"{100 * r.read_rate:.1f}%"
        lines.append(
            f"| {r.name} | {r.engine} | {r.decoded}/{len(r.results)} | {rate} "
            f"| {len(r.recovered)} | {len(r.regressions)} | {r.wall_s:.2f} |"
        )
    lines += _variant_delta(reports)
    for r in reports:
        lines += ["", f"## {r.name} ({r.engine})"]
        attr = r.attribution
        if attr:
            lines.append(
                "Decoder attribution: "
                + ", ".join(f"{k}={v}" for k, v in attr.items())
            )
        if r.recovered:
            lines.append("Recovery candidates (hypothetical) — recorded as a live miss:")
            for br in r.recovered:
                lines.append(
                    f"  - {br.candidate_id}: {br.decoded_payload!r} via "
                    f"{br.decoder} (frame {br.frames_to_decode})"
                )
        if r.regressions:
            lines.append(
                "Not reproduced — recorded as a live pass, no decode on replay "
                "(usually the coarser replay ROI, not a config failure; check "
                "the variant delta above):"
            )
            for br in r.regressions:
                lines.append(
                    f"  - {br.candidate_id}: ground truth {br.ground_truth} "
                    f"-> {br.decoded_payload!r}"
                )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("recording_dir", type=Path, help="a SegmentRecorder output dir")
    ap.add_argument(
        "--config", action="append", default=[],
        help="AppConfig YAML to replay under (repeatable; variant = file stem)",
    )
    ap.add_argument(
        "--base", type=Path, default=None,
        help="base config for the default legacy-vs-zxing comparison",
    )
    ap.add_argument("--out", type=Path, default=None, help="write the report markdown here")
    args = ap.parse_args(argv)

    if not args.recording_dir.is_dir():
        print(f"replay: {args.recording_dir} is not a directory", file=sys.stderr)
        return 2
    bursts = find_bursts(args.recording_dir)
    if not bursts:
        print(f"replay: no bursts found under {args.recording_dir}", file=sys.stderr)
        return 1

    reports = [score_variant(name, cfg, bursts) for name, cfg in _variants(args)]
    report_md = render_report(reports, len(bursts))
    print(report_md)
    if args.out is not None:
        args.out.write_text(report_md, encoding="utf-8")
        print(f"report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
