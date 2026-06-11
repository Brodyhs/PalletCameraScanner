"""24/7 soak harness: flat memory, zero unhandled exceptions, injected
failures with supervisor restart and no event loss (spec §11).

Load modes:
  replay     loop a recorded clip unpaced (records one first if --clip absent)
  synthetic  long generated run (lazy pass planning keeps memory bounded)

Run:
  python tools/soak.py --minutes 120 --mode replay
  python tools/soak.py --minutes 3 --mode synthetic --inject-every-s 30

Failure injection (synthetic mode): a FlakySource raises after every N
source-seconds; the pipeline dies through its flush path (pending misses
become events), this harness restarts the run — the crash-only half of the
recovery contract; the in-process watchdog is Phase 3. No event loss is
proven two ways: per-segment truth reconciliation, and an HTTP-sink outbox
pointed at a dead endpoint whose rows must equal every emitted event id
across all restarts.

Memory: RSS sampled via psutil (a [dev] extra; the runtime package never
imports it). After --warmup-s, the least-squares slope must stay under
--max-slope-mb-per-min and the final RSS within 1.3x the post-warmup
baseline.
"""

from __future__ import annotations

import argparse
import gc
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.app import PipelineRunner, Reconciliation, reconcile_truth
from palletscan.config import AppConfig, apply_overrides, load_config
from palletscan.logging_setup import setup_logging
from palletscan.reliability.flaky import FlakySource, InjectedFailure
from palletscan.sources.factory import create_source
from palletscan.sources.record import record_synthetic_clip
from palletscan.sources.synthetic import SyntheticSource

#: Connection-refused-immediately endpoint for outbox-accumulation runs.
DEAD_URL = "http://127.0.0.1:1/events"

#: Passes "remaining" in a duration-driven synthetic run; the deadline
#: timer stops the runner long before the source exhausts.
UNBOUNDED_PASSES = 10_000_000

RSS_RATIO_MAX = 1.3


class RssSampler(threading.Thread):
    """Samples this process's RSS (MB) and CPU%% on a fixed cadence."""

    def __init__(self, interval_s: float) -> None:
        super().__init__(name="rss-sampler", daemon=True)
        self._interval_s = interval_s
        self._stop = threading.Event()
        self.samples: list[tuple[float, float]] = []  # (monotonic_s, rss_mb)
        self.cpu: list[float] = []

    def run(self) -> None:
        import psutil  # [dev] extra — soak harness only

        proc = psutil.Process()
        proc.cpu_percent(None)  # prime the delta-based meter
        while not self._stop.wait(self._interval_s):
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            self.samples.append((time.monotonic(), rss_mb))
            self.cpu.append(proc.cpu_percent(None))

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=self._interval_s + 2)


@dataclass(slots=True)
class MemoryVerdict:
    enough_samples: bool
    slope_mb_per_min: float
    baseline_mb: float
    final_mb: float
    peak_mb: float
    problems: list[str]


def analyze_rss(
    samples: list[tuple[float, float]],
    warmup_s: float,
    max_slope_mb_per_min: float,
) -> MemoryVerdict:
    """Least-squares slope + final/baseline ratio over post-warmup samples."""
    if not samples:
        return MemoryVerdict(False, 0.0, 0.0, 0.0, 0.0, ["no RSS samples"])
    t0 = samples[0][0]
    post = [(t - t0, mb) for t, mb in samples if t - t0 >= warmup_s]
    peak = max(mb for _, mb in samples)
    if len(post) < 5:
        return MemoryVerdict(
            False, 0.0, 0.0, 0.0, peak,
            [f"only {len(post)} post-warmup samples (need >= 5)"],
        )
    n = len(post)
    mean_t = sum(t for t, _ in post) / n
    mean_m = sum(m for _, m in post) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in post)
    cov = sum((t - mean_t) * (m - mean_m) for t, m in post)
    slope_per_min = (cov / var_t) * 60.0 if var_t > 0 else 0.0
    k = min(5, n)
    baseline = sorted(m for _, m in post[:k])[k // 2]
    final = sorted(m for _, m in post[-k:])[k // 2]
    problems = []
    if slope_per_min > max_slope_mb_per_min:
        problems.append(
            f"RSS slope {slope_per_min:.3f} MB/min exceeds "
            f"{max_slope_mb_per_min} MB/min"
        )
    if final > baseline * RSS_RATIO_MAX:
        problems.append(
            f"final RSS {final:.1f} MB exceeds {RSS_RATIO_MAX}x post-warmup "
            f"baseline {baseline:.1f} MB"
        )
    return MemoryVerdict(True, slope_per_min, baseline, final, peak, problems)


@dataclass(slots=True)
class Segment:
    injected: bool
    frames: int
    events: int
    frame_errors: int
    sink_errors: int
    reconciliation: Reconciliation | None


@dataclass(slots=True)
class SoakReport:
    duration_s: float
    segments: list[Segment] = field(default_factory=list)
    restarts: int = 0
    max_gap_s: float = 0.0
    memory: MemoryVerdict | None = None
    cpu_avg: float = 0.0
    cpu_max: float = 0.0
    outbox_check: str = "skipped"
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def format(self) -> str:
        frames = sum(s.frames for s in self.segments)
        events = sum(s.events for s in self.segments)
        decoded = sum(
            s.reconciliation.decoded for s in self.segments if s.reconciliation
        )
        truth = sum(
            s.reconciliation.truth_passes for s in self.segments if s.reconciliation
        )
        lines = [
            "── soak report ──",
            f"duration         : {self.duration_s / 60:.1f} min "
            f"({len(self.segments)} segment(s), {self.restarts} injected restarts, "
            f"max restart gap {self.max_gap_s:.2f}s)",
            f"frames / events  : {frames} / {events}",
            f"truth accounting : {decoded}/{truth} decoded, "
            f"{sum(s.reconciliation.missed for s in self.segments if s.reconciliation)}"
            " missed-with-evidence",
            f"cpu              : avg {self.cpu_avg:.0f}% max {self.cpu_max:.0f}%",
            f"outbox           : {self.outbox_check}",
        ]
        if self.memory is not None:
            m = self.memory
            lines.append(
                f"rss              : baseline {m.baseline_mb:.1f} MB -> final "
                f"{m.final_mb:.1f} MB (peak {m.peak_mb:.1f}), slope "
                f"{m.slope_mb_per_min:+.3f} MB/min"
            )
        lines.append(
            "verdict          : " + ("OK" if self.ok else "FAIL")
        )
        lines.extend(f"  PROBLEM: {p}" for p in self.problems)
        return "\n".join(lines)


def build_config(args: argparse.Namespace) -> AppConfig:
    cfg = load_config(args.config)
    cfg = apply_overrides(cfg, seed=args.seed, data_dir=Path(args.data_dir))
    sinks: dict = {
        "console": cfg.sinks.console.model_copy(update={"enabled": False}),
        "sqlite": cfg.sinks.sqlite.model_copy(update={"enabled": False}),
    }
    http_url = args.http_url
    if http_url is None and args.inject_every_s:
        http_url = DEAD_URL  # accumulate-only outbox proves zero loss
    if http_url is not None:
        sinks["http"] = cfg.sinks.http.model_copy(
            update={"enabled": True, "url": http_url}
        )
    update: dict = {"sinks": cfg.sinks.model_copy(update=sinks)}
    if args.mode == "synthetic":
        update["synthetic"] = cfg.synthetic.model_copy(
            update={"num_passes": UNBOUNDED_PASSES}
        )
    else:
        clip = Path(args.clip) if args.clip else None
        if clip is None:
            clip = Path(args.data_dir) / "soak_clip.avi"
            record_cfg = apply_overrides(cfg, num_passes=args.clip_passes)
            print(f"recording {args.clip_passes}-pass soak clip to {clip} ...")
            record_synthetic_clip(record_cfg, clip)
        update["source"] = cfg.source.model_copy(update={"type": "video"})
        update["video"] = cfg.video.model_copy(
            update={"path": clip, "speed": 0.0, "loop": 0}
        )
    return cfg.model_copy(update=update)


def _segment_from(runner: PipelineRunner, injected: bool) -> Segment:
    snap = runner.metrics.snapshot()
    inner = (
        runner.source.inner
        if isinstance(runner.source, FlakySource)
        else runner.source
    )
    reconciliation = None
    if isinstance(inner, SyntheticSource):
        # Truth holds only *completed* passes, so a mid-pass injection
        # cannot leave a half-seen pass unaccounted-but-unchecked: the
        # open segment's flush-miss covers it, and completed passes must
        # all reconcile.
        reconciliation = reconcile_truth(
            inner.truth, runner.collected_events, inner.nominal_fps
        )
    return Segment(
        injected=injected,
        frames=snap["frames"]["processed"],
        events=snap["events"]["handled"],
        frame_errors=snap["frames"]["errors"],
        sink_errors=snap["events"]["sink_errors"],
        reconciliation=reconciliation,
    )


def _check_outbox(cfg: AppConfig, emitted_ids: list[str]) -> tuple[str, list[str]]:
    """Dead-endpoint mode: outbox rows must be exactly the emitted ids."""
    path = Path(cfg.sinks.http.outbox_path)
    conn = sqlite3.connect(path)
    try:
        rows = {r[0] for r in conn.execute("SELECT event_id FROM outbox")}
    finally:
        conn.close()
    emitted = set(emitted_ids)
    problems = []
    if rows != emitted:
        problems.append(
            f"outbox/emitted mismatch: {len(emitted - rows)} missing, "
            f"{len(rows - emitted)} unexpected"
        )
    return f"{len(rows)} rows == {len(emitted)} emitted ids", problems


def run_soak(args: argparse.Namespace) -> SoakReport:
    cfg = build_config(args)
    dead_url_mode = cfg.sinks.http.enabled and cfg.sinks.http.url == DEAD_URL
    if dead_url_mode:
        # The outbox accounting check compares against THIS run's events;
        # rows persisted by a previous soak (never drained — the endpoint
        # is dead by design) would read as fabricated loss. The harness
        # owns this path, so start it fresh.
        for suffix in ("", "-wal", "-shm"):
            Path(f"{cfg.sinks.http.outbox_path}{suffix}").unlink(missing_ok=True)
    duration_s = args.minutes * 60.0
    report = SoakReport(duration_s=duration_s)
    sampler = RssSampler(args.rss_interval_s)
    sampler.start()
    deadline = time.monotonic() + duration_s
    emitted_ids: list[str] = []
    outage_began_at: float | None = None
    try:
        while time.monotonic() < deadline:
            source = create_source(cfg)
            if args.inject_every_s:
                raise_at = int(args.inject_every_s * (source.nominal_fps or 30.0))
                source = FlakySource(source, raise_at=raise_at)
            runner = PipelineRunner.from_config(cfg, source=source)
            timer = threading.Timer(
                max(0.1, deadline - time.monotonic()), runner.stop
            )
            timer.daemon = True
            timer.start()
            injected = False
            try:
                runner.run(stats_interval_s=args.stats_interval)
            except RuntimeError as exc:
                if not isinstance(exc.__cause__, InjectedFailure):
                    raise  # a real failure must fail the soak loudly
                injected = True
                report.restarts += 1
            finally:
                timer.cancel()
            # Restart gap = injection instant (before the dying run's
            # drain/flush/sink teardown) -> first frame ingested by THIS
            # runner. Measuring construction-to-construction would skip
            # the expensive half of the outage.
            if outage_began_at is not None:
                first_wall = runner.metrics.first_frame_wall
                if first_wall is not None:
                    report.max_gap_s = max(
                        report.max_gap_s, first_wall - outage_began_at
                    )
                outage_began_at = None
            if injected and isinstance(source, FlakySource):
                outage_began_at = source.failed_wall or time.monotonic()
            seg = _segment_from(runner, injected)
            emitted_ids.extend(e.event_id for e in runner.collected_events)
            if runner.collected_events_dropped:
                report.problems.append(
                    f"segment {len(report.segments) + 1}: event collector "
                    f"overflowed by {runner.collected_events_dropped}; "
                    "accounting is incomplete (shorten segments)"
                )
            report.segments.append(seg)
            if seg.frame_errors:
                report.problems.append(
                    f"segment {len(report.segments)}: {seg.frame_errors} frame errors"
                )
            if seg.sink_errors:
                report.problems.append(
                    f"segment {len(report.segments)}: {seg.sink_errors} sink errors"
                )
            if seg.reconciliation and seg.reconciliation.unaccounted:
                report.problems.append(
                    f"segment {len(report.segments)}: unaccounted passes "
                    f"{seg.reconciliation.unaccounted}"
                )
            # A production restart is a fresh process; the in-harness
            # equivalent of that boundary is collecting the dead runner's
            # cyclic graph (tracker<->bus<->buffer hold ~MBs of frames)
            # now instead of whenever gen-2 gc would get to it.
            del runner, source
            gc.collect()
    finally:
        sampler.stop()

    report.memory = analyze_rss(
        sampler.samples, args.warmup_s, args.max_slope_mb_per_min
    )
    report.problems.extend(report.memory.problems)
    if sampler.cpu:
        report.cpu_avg = sum(sampler.cpu) / len(sampler.cpu)
        report.cpu_max = max(sampler.cpu)
    if args.inject_every_s and report.restarts == 0:
        report.problems.append("failure injection requested but never triggered")
    if report.max_gap_s >= 10.0:
        report.problems.append(f"restart gap {report.max_gap_s:.1f}s >= 10s")
    if dead_url_mode:
        report.outbox_check, outbox_problems = _check_outbox(cfg, emitted_ids)
        report.problems.extend(outbox_problems)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    dur = ap.add_mutually_exclusive_group(required=True)
    dur.add_argument("--minutes", type=float, help="soak duration in minutes")
    dur.add_argument("--hours", type=float, help="soak duration in hours")
    ap.add_argument("--mode", choices=("replay", "synthetic"), default="replay")
    ap.add_argument("--config", type=Path, default=None, help="YAML config path")
    ap.add_argument("--data-dir", type=Path, default=Path("data/soak"))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--clip", type=Path, default=None, help="replay-mode clip")
    ap.add_argument(
        "--clip-passes", type=int, default=40, help="passes when auto-recording"
    )
    ap.add_argument(
        "--inject-every-s",
        type=float,
        default=None,
        help="inject a source failure every N source-seconds (synthetic mode)",
    )
    ap.add_argument("--http-url", default=None, help="HTTP sink endpoint")
    ap.add_argument("--rss-interval-s", type=float, default=2.0)
    ap.add_argument("--warmup-s", type=float, default=60.0)
    ap.add_argument("--max-slope-mb-per-min", type=float, default=1.0)
    ap.add_argument("--stats-interval", type=float, default=60.0)
    args = ap.parse_args(argv)
    if args.hours is not None:
        args.minutes = args.hours * 60.0
    if args.inject_every_s and args.mode != "synthetic":
        ap.error("--inject-every-s requires --mode synthetic (needs ground truth)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging("WARNING")
    report = run_soak(args)
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
