"""24/7 soak harness: flat memory, zero unhandled exceptions, injected
failures with supervisor restart and no event loss (spec §11).

Load modes:
  replay     loop a recorded clip unpaced (records one first if --clip absent)
  synthetic  long generated run (lazy pass planning keeps memory bounded)
  inject     hours-long unbounded CameraInjectionSource run on the LIVE camera
             — a MANUAL bench gate (holds the real camera exclusively), with
             periodic pipeline-health snapshots and drift detection over time.

Run:
  python tools/soak.py --minutes 120 --mode replay
  python tools/soak.py --minutes 3 --mode synthetic --inject-every-s 30
  python tools/soak.py --hours 8 --mode inject --camera cam-color --exposure-ms 4 --snapshot-interval-s 300

Failure injection (synthetic mode): a FlakySource raises after every N
source-seconds; the pipeline dies through its flush path (pending misses
become events), this harness restarts the run — the crash-only half of the
recovery contract; the in-process watchdog is Phase 3. No event loss is
proven two ways: per-segment truth reconciliation, and an HTTP-sink outbox
pointed at a dead endpoint whose rows must equal every emitted event id
across all restarts.

Memory: RSS sampled via psutil (a [dev] extra; the runtime package never
imports it). After warmup — adaptive knee detection by default
(detect_warmup; --warmup-s pins it explicitly) — the least-squares slope
must stay under --max-slope-mb-per-min and the final RSS within 1.3x the
post-warmup baseline. A genuine leak never plateaus, so adaptive warmup
rides its max bound and the slope gate fails, as it should.
"""

from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.app import PipelineRunner, Reconciliation, reconcile_truth
from palletscan.config import AppConfig, apply_overrides, load_config
from palletscan.logging_setup import setup_logging
from palletscan.reliability.flaky import FlakySource, InjectedFailure
from palletscan.sources.factory import create_source
from palletscan.sources.inject import CameraInjectionSource
from palletscan.sources.record import record_synthetic_clip
from palletscan.sources.synthetic import SyntheticSource

#: Connection-refused-immediately endpoint for outbox-accumulation runs.
DEAD_URL = "http://127.0.0.1:1/events"

#: Passes "remaining" in a duration-driven synthetic run; the deadline
#: timer stops the runner long before the source exhausts.
UNBOUNDED_PASSES = 10_000_000

RSS_RATIO_MAX = 1.3

#: File name of the inject-soak snapshot series (one JSON line each). The
#: full path is derived from the resolved --data-dir in run_soak, like every
#: other soak artifact — a hardcoded path would ignore the override.
SNAPSHOTS_FILENAME = "snapshots.jsonl"

#: Minimum number of snapshots WITH a real read_rate_1h (the metric is None
#: until the rolling 1h window fills) before the drift gate is meaningful; a
#: shorter series reports "insufficient" instead of failing.
MIN_DRIFT_SNAPSHOTS = 3

#: Snapshots dropped from the head of the drift fit — pipeline warmup (the 1h
#: window is still filling, latency settling) would bias the slope.
DRIFT_WARMUP_SNAPSHOTS = 1


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


class SnapshotSampler(threading.Thread):
    """Periodically snapshots the LIVE runner's metrics and appends one JSON
    line per sample to ``path`` (``<data-dir>/snapshots.jsonl``) for drift
    analysis.

    The runner is recreated per soak segment, so the sampler is handed a
    *getter* (not a runner ref) and reads ``get_runner()`` each tick to
    sample whichever runner is currently live. Each line is flushed so a
    crash mid-run still leaves a complete-up-to-here series on disk.
    """

    def __init__(
        self,
        interval_s: float,
        get_runner: Callable[[], PipelineRunner | None],
        rss_sampler: RssSampler | None = None,
        *,
        path: Path,
    ) -> None:
        super().__init__(name="snapshot-sampler", daemon=True)
        self._interval_s = interval_s
        self._get_runner = get_runner
        self._rss_sampler = rss_sampler
        self._path = path
        self._stop = threading.Event()
        # NOT "_started": threading.Thread owns that attribute (an Event its
        # start() checks); shadowing it with a float breaks Thread.start().
        self._t0 = time.monotonic()
        #: In-memory mirror of the lines written, for post-run drift analysis
        #: (so the gate never has to re-read/parse the file).
        self.snapshots: list[dict[str, Any]] = []

    def _last_rss_mb(self) -> float | None:
        if self._rss_sampler and self._rss_sampler.samples:
            return self._rss_sampler.samples[-1][1]
        return None

    def _sample_once(self) -> None:
        runner = self._get_runner()
        if runner is None:
            return  # between segments — nothing live to sample
        snap = runner.metrics.snapshot()
        row = {
            "wall": time.time(),
            "uptime_s": round(time.monotonic() - self._t0, 3),
            "fps": snap["fps"],
            "read_rate_1h": snap["read_rate_1h"],
            "passes_per_hour": snap["passes"]["per_hour"],
            "decode_p50_ms": snap["decode"]["p50_ms"],
            "decode_p95_ms": snap["decode"]["p95_ms"],
            "frames_dropped": snap["frames"]["dropped"],
            "frames_errors": snap["frames"]["errors"],
            "rss_mb": self._last_rss_mb(),
        }
        self.snapshots.append(row)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()

    def run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._sample_once()
            except Exception:  # noqa: BLE001 — a sampler must never kill the soak
                pass

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
    warmup_used_s: float
    problems: list[str]


def _ls_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope dy/dx of (x, y) points in the points' own units.

    Returns 0.0 when x has no variance (a single distinct x, or one point);
    callers add their own unit scale (e.g. *60 for per-min, *3600 for
    per-hour) on top.
    """
    n = len(points)
    if n < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in points)
    if var_x <= 0:
        return 0.0
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return cov / var_x


def _ls_slope_mb_per_min(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (seconds, MB) points, in MB/min."""
    return _ls_slope(points) * 60.0


def detect_warmup(
    samples: list[tuple[float, float]],
    window_s: float = 60.0,
    slope_thresh_mb_per_min: float = 2.0,
    min_warmup_s: float = 45.0,
    max_warmup_frac: float = 0.5,
) -> float:
    """Adaptive warmup: the earliest time the RSS curve plateaus.

    Returns the smallest ``t`` where the least-squares slope over
    ``[t, t + window_s]`` drops below the threshold, clamped to
    ``[min_warmup_s, max_warmup_frac * duration]``. Allocator settling and
    the macOS lazy page-reclaim ramp (ASSUMPTIONS #39) vary by machine and
    OS — measuring the knee beats guessing per-OS constants. A genuine
    leak never plateaus, so it rides the max bound and the slope gate
    downstream fails on the remaining half of the run, as it should.
    """
    if not samples:
        return min_warmup_s
    t0 = samples[0][0]
    rel = [(t - t0, mb) for t, mb in samples]
    max_warmup = rel[-1][0] * max_warmup_frac
    if max_warmup <= min_warmup_s:
        return min_warmup_s
    for t_start, _ in rel:
        if t_start < min_warmup_s:
            continue
        if t_start > max_warmup:
            break
        window = [p for p in rel if t_start <= p[0] <= t_start + window_s]
        if len(window) < 5:
            break
        if _ls_slope_mb_per_min(window) < slope_thresh_mb_per_min:
            return t_start
    return max_warmup


def analyze_rss(
    samples: list[tuple[float, float]],
    warmup_s: float | None,
    max_slope_mb_per_min: float,
) -> MemoryVerdict:
    """Least-squares slope + final/baseline ratio over post-warmup samples.

    ``warmup_s=None`` detects the warmup adaptively (:func:`detect_warmup`);
    an explicit value is honored unchanged. The verdict records what was
    used (``warmup_used_s``) so runs stay comparable.
    """
    if not samples:
        return MemoryVerdict(False, 0.0, 0.0, 0.0, 0.0, 0.0, ["no RSS samples"])
    used_warmup = detect_warmup(samples) if warmup_s is None else warmup_s
    t0 = samples[0][0]
    post = [(t - t0, mb) for t, mb in samples if t - t0 >= used_warmup]
    peak = max(mb for _, mb in samples)
    if len(post) < 5:
        return MemoryVerdict(
            False, 0.0, 0.0, 0.0, peak, used_warmup,
            [f"only {len(post)} post-warmup samples (need >= 5)"],
        )
    n = len(post)
    slope_per_min = _ls_slope_mb_per_min(post)
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
    return MemoryVerdict(
        True, slope_per_min, baseline, final, peak, used_warmup, problems
    )


@dataclass(slots=True)
class DriftVerdict:
    enough_snapshots: bool
    n_total: int
    n_fit: int
    read_rate_start: float | None
    read_rate_end: float | None
    read_rate_slope_pp_per_hour: float
    p95_start: float | None
    p95_end: float | None
    p95_slope_ms_per_hour: float
    problems: list[str] = field(default_factory=list)
    note: str = ""


def analyze_drift(
    snapshots: list[dict[str, Any]],
    max_read_rate_drop_per_hour: float,
    max_p95_rise_per_hour: float,
    warmup_snapshots: int = DRIFT_WARMUP_SNAPSHOTS,
    min_snapshots: int = MIN_DRIFT_SNAPSHOTS,
) -> DriftVerdict:
    """Least-squares drift of read_rate_1h (pp/hour) and decode p95 (ms/hour)
    over POST-WARMUP snapshots, gated against the per-hour bounds.

    ``read_rate_1h`` is ``None`` until the rolling 1h window first fills, so
    only snapshots with a real value count toward the read-rate fit. When
    fewer than ``min_snapshots`` such points exist the gate is SKIPPED (the
    verdict reports "insufficient" rather than failing). The read-rate slope
    is reported in percentage *points* per hour (read_rate is a 0..1 ratio,
    so the per-hour slope is scaled by 100).
    """
    n_total = len(snapshots)
    post = snapshots[warmup_snapshots:] if warmup_snapshots else list(snapshots)

    # read_rate_1h fit: only points where the metric is populated.
    rr_pts = [
        (s["uptime_s"], s["read_rate_1h"])
        for s in post
        if s.get("read_rate_1h") is not None
    ]
    # decode p95 fit: only points where a latency percentile exists.
    p95_pts = [
        (s["uptime_s"], s["decode_p95_ms"])
        for s in post
        if s.get("decode_p95_ms") is not None
    ]

    if len(rr_pts) < min_snapshots:
        return DriftVerdict(
            enough_snapshots=False,
            n_total=n_total,
            n_fit=len(rr_pts),
            read_rate_start=(rr_pts[0][1] if rr_pts else None),
            read_rate_end=(rr_pts[-1][1] if rr_pts else None),
            read_rate_slope_pp_per_hour=0.0,
            p95_start=(p95_pts[0][1] if p95_pts else None),
            p95_end=(p95_pts[-1][1] if p95_pts else None),
            p95_slope_ms_per_hour=0.0,
            problems=[],
            note=(
                f"insufficient snapshots for drift "
                f"({len(rr_pts)} with read_rate_1h, need >= {min_snapshots})"
            ),
        )

    # pp/hour: read_rate is a 0..1 ratio -> *100 for percentage points, *3600
    # for per-hour (uptime_s is in seconds).
    rr_slope_pp_hr = _ls_slope(rr_pts) * 100.0 * 3600.0
    p95_slope_ms_hr = _ls_slope(p95_pts) * 3600.0 if len(p95_pts) >= 2 else 0.0

    problems: list[str] = []
    # A FALLING read rate is the danger: slope below -max_drop fails.
    if rr_slope_pp_hr < -abs(max_read_rate_drop_per_hour):
        problems.append(
            f"read_rate_1h drifting down {rr_slope_pp_hr:+.2f} pp/hr "
            f"(limit -{abs(max_read_rate_drop_per_hour):.2f} pp/hr)"
        )
    # A RISING p95 is the danger: slope above +max_rise fails.
    if p95_slope_ms_hr > abs(max_p95_rise_per_hour):
        problems.append(
            f"decode p95 drifting up {p95_slope_ms_hr:+.2f} ms/hr "
            f"(limit +{abs(max_p95_rise_per_hour):.2f} ms/hr)"
        )

    return DriftVerdict(
        enough_snapshots=True,
        n_total=n_total,
        n_fit=len(rr_pts),
        read_rate_start=rr_pts[0][1],
        read_rate_end=rr_pts[-1][1],
        read_rate_slope_pp_per_hour=rr_slope_pp_hr,
        p95_start=(p95_pts[0][1] if p95_pts else None),
        p95_end=(p95_pts[-1][1] if p95_pts else None),
        p95_slope_ms_per_hour=p95_slope_ms_hr,
        problems=problems,
        note="",
    )


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
    drift: DriftVerdict | None = None
    snapshots_path: Path | None = None
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
                f"{m.slope_mb_per_min:+.3f} MB/min "
                f"(warmup {m.warmup_used_s:.0f}s)"
            )
        if self.drift is not None:
            d = self.drift
            path = self.snapshots_path
            lines.append("── drift ──")
            if not d.enough_snapshots:
                lines.append(f"  {d.note}")
            else:

                def _pct(v: float | None) -> str:
                    return "n/a" if v is None else f"{v * 100:.1f}%"

                def _ms(v: float | None) -> str:
                    return "n/a" if v is None else f"{v:.1f}ms"

                lines.append(
                    f"  read_rate 1h : {_pct(d.read_rate_start)} -> "
                    f"{_pct(d.read_rate_end)} "
                    f"(slope {d.read_rate_slope_pp_per_hour:+.2f} pp/hr)"
                )
                lines.append(
                    f"  decode p95   : {_ms(d.p95_start)} -> "
                    f"{_ms(d.p95_end)} "
                    f"(slope {d.p95_slope_ms_per_hour:+.2f} ms/hr)"
                )
            lines.append(f"  snapshots    : {d.n_total} -> {path}")
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
    if args.mode == "inject":
        # The CameraInjectionSource is built directly in run_soak (it owns the
        # real camera); here we only select the camera and make the synthetic
        # pass stream effectively-unbounded so the DEADLINE timer — not pass
        # exhaustion — ends the run.
        if args.camera is not None:
            update["source"] = cfg.source.model_copy(
                update={"camera": args.camera}
            )
        update["synthetic"] = cfg.synthetic.model_copy(
            update={"num_passes": UNBOUNDED_PASSES}
        )
    elif args.mode == "synthetic":
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
    # The runner is recreated per segment; the snapshot sampler reads this
    # holder each tick so it always samples whichever runner is live.
    live_runner: dict[str, PipelineRunner | None] = {"runner": None}
    snap_sampler: SnapshotSampler | None = None
    if args.mode == "inject":
        snapshots_path = Path(args.data_dir) / SNAPSHOTS_FILENAME
        report.snapshots_path = snapshots_path
        # Fresh file per run: cross-run appends interleave two series and
        # corrupt offline drift analysis (in-run writes still append
        # line-by-line for crash robustness).
        snapshots_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots_path.write_text("", encoding="utf-8")
        snap_sampler = SnapshotSampler(
            args.snapshot_interval_s,
            get_runner=lambda: live_runner["runner"],
            rss_sampler=sampler,
            path=snapshots_path,
        )
        snap_sampler.start()
    deadline = time.monotonic() + duration_s
    emitted_ids: list[str] = []
    outage_began_at: float | None = None
    try:
        while time.monotonic() < deadline:
            if args.mode == "inject":
                # CameraInjectionSource holds the real camera exclusively; the
                # unbounded synthetic stream is bounded by the deadline timer.
                source = CameraInjectionSource(
                    cfg.synthetic,
                    cfg,
                    exposure_s=args.exposure_ms / 1000.0,
                )
            else:
                source = create_source(cfg)
            if args.inject_every_s:
                raise_at = int(args.inject_every_s * (source.nominal_fps or 30.0))
                source = FlakySource(source, raise_at=raise_at)
            runner = PipelineRunner.from_config(cfg, source=source)
            live_runner["runner"] = runner
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
            live_runner["runner"] = None  # don't sample a torn-down runner
            del runner, source
            gc.collect()
    finally:
        if snap_sampler is not None:
            snap_sampler.stop()
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
    if snap_sampler is not None:
        report.drift = analyze_drift(
            snap_sampler.snapshots,
            args.max_read_rate_drop_per_hour,
            args.max_p95_rise_per_hour,
        )
        # Only a *populated* drift fit can fail the soak; a too-short series
        # reports "insufficient" (see analyze_drift) and never gates.
        report.problems.extend(report.drift.problems)
    if dead_url_mode:
        report.outbox_check, outbox_problems = _check_outbox(cfg, emitted_ids)
        report.problems.extend(outbox_problems)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    dur = ap.add_mutually_exclusive_group(required=True)
    dur.add_argument("--minutes", type=float, help="soak duration in minutes")
    dur.add_argument("--hours", type=float, help="soak duration in hours")
    ap.add_argument(
        "--mode", choices=("replay", "synthetic", "inject"), default="replay"
    )
    ap.add_argument(
        "--camera",
        default=None,
        help="cameras[].id to inject onto (inject mode; required when >1 "
        "camera is configured)",
    )
    ap.add_argument(
        "--exposure-ms",
        type=float,
        default=4.0,
        help="modeled field shutter driving injected motion blur (inject mode)",
    )
    ap.add_argument(
        "--snapshot-interval-s",
        type=float,
        default=300.0,
        help="seconds between pipeline-health snapshots (inject mode)",
    )
    ap.add_argument(
        "--max-read-rate-drop-per-hour",
        type=float,
        default=2.0,
        help="fail if read_rate_1h drifts down faster than this (pp/hour)",
    )
    ap.add_argument(
        "--max-p95-rise-per-hour",
        type=float,
        default=5.0,
        help="fail if decode p95 drifts up faster than this (ms/hour)",
    )
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
    ap.add_argument(
        "--warmup-s",
        type=float,
        default=None,
        help="post-start seconds excluded from the memory verdict "
        "(default: adaptive knee detection; see detect_warmup)",
    )
    ap.add_argument("--max-slope-mb-per-min", type=float, default=1.0)
    ap.add_argument("--stats-interval", type=float, default=60.0)
    args = ap.parse_args(argv)
    if args.hours is not None:
        args.minutes = args.hours * 60.0
    if args.inject_every_s and args.mode == "inject":
        # inject mode holds the real camera exclusively — it is a manual bench
        # gate, not a CI failure-injection run. There is also no FlakySource
        # ground-truth contract for a live feed.
        ap.error(
            "--inject-every-s is not allowed in --mode inject "
            "(it holds the real camera; failure injection is for synthetic)"
        )
    if args.inject_every_s and args.mode != "synthetic":
        ap.error("--inject-every-s requires --mode synthetic (needs ground truth)")
    return args


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass  # the drift block's box-drawing chars die on a cp1252 console
    args = parse_args(argv)
    setup_logging("WARNING")
    report = run_soak(args)
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
