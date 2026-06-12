"""Spec §11 CPU measurement: sustained CPU on a typical 4-core desktop
must stay <= ~50% under burst load, dashboard functional throughout.

Method (D10): **replay of a dense recorded burst clip at 1.0x speed**,
child processes sampled via psutil. Replay, not realtime synthetic —
VideoFileSource paces on an absolute schedule (no per-frame sleep drift)
and its MJPG-decode-per-frame cost is the closest proxy for live MJPEG
UVC ingest, while synthetic rendering cost doesn't exist in production.
The burst clip tightens idle gaps to ~0.2-0.8 s -> ~50 passes/min, ~7x
the 7 passes/min spec average.

Scenarios:

  baseline   one replay child, dashboard off
  station    two replay children (the A/B approximation; separate
             --data-dirs so instance locks don't collide) + dashboard on
             + one live MJPEG client streaming /live/video0

Each child is sampled with ``psutil.Process.cpu_percent()`` at 1 Hz for
>= 5 min; the report shows avg/p95/max, raw (sum-over-cores %) and
normalized to a 4-core budget (raw / 4). psutil is a [dev] extra — this
tool never ships in the runtime package.

Run (from the repo root, venv active):

  python tools/measure_cpu.py                      # both scenarios, 5 min each
  python tools/measure_cpu.py --scenario baseline --seconds 60

Numbers from the dev Mac are indicative; the factory box run
(ARRIVAL_CHECKLIST) is authoritative.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import AppConfig, load_config
from palletscan.sources.record import record_synthetic_clip

#: Burst idle gaps between passes (vs the default 0.3-1.5 s): with the
#: ~0.5-0.7 s in-frame dwell this yields ~50 passes/min.
BURST_IDLE_S = (0.2, 0.8)

NORMALIZE_CORES = 4


def burst_config(cfg: AppConfig, passes: int) -> AppConfig:
    """The recording scenario: spec envelope, idle gaps tightened to burst."""
    return cfg.model_copy(
        update={
            "synthetic": cfg.synthetic.model_copy(
                update={"num_passes": passes, "idle_s_range": BURST_IDLE_S}
            )
        }
    )


@dataclass(frozen=True, slots=True)
class CpuSummary:
    label: str
    samples: int
    avg_pct: float
    p95_pct: float
    max_pct: float


def summarize(label: str, samples: list[float]) -> CpuSummary:
    """avg/p95/max of raw psutil cpu_percent samples (sum over cores)."""
    if not samples:
        return CpuSummary(label, 0, 0.0, 0.0, 0.0)
    ordered = sorted(samples)
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    return CpuSummary(
        label=label,
        samples=len(samples),
        avg_pct=sum(samples) / len(samples),
        p95_pct=p95,
        max_pct=ordered[-1],
    )


def sum_per_sample(series: list[list[float]]) -> list[float]:
    """Pairwise sum across children (truncated to the shortest series) —
    the station's total CPU at each sampling instant."""
    if not series:
        return []
    n = min(len(s) for s in series)
    return [sum(s[i] for s in series) for i in range(n)]


def render_report(
    scenario: str, summaries: list[CpuSummary], cores: int = NORMALIZE_CORES
) -> str:
    """Markdown section: raw % and the spec's 4-core-budget normalization."""
    lines = [
        f"### Scenario: {scenario}",
        "",
        "| process | samples | avg % | p95 % | max % | "
        f"avg /{cores}-core | p95 /{cores}-core |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.label} | {s.samples} | {s.avg_pct:.1f} | {s.p95_pct:.1f} "
            f"| {s.max_pct:.1f} | {s.avg_pct / cores:.1f}% | "
            f"{s.p95_pct / cores:.1f}% |"
        )
    lines.append("")
    return "\n".join(lines)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_child_config(path: Path, port: int | None) -> None:
    """Quiet sinks for an unattended child; fixed dashboard port if any."""
    raw: dict = {
        "sinks": {"console": {"enabled": False}},
        "logging": {"level": "WARNING"},
    }
    if port is not None:
        raw["web"] = {"port": port}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")


def _spawn_replay(
    clip: Path, config: Path, data_dir: Path, dashboard: bool
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "palletscan",
        "replay",
        str(clip),
        "--speed",
        "1.0",
        "--loop",
        "0",
        "--config",
        str(config),
        "--data-dir",
        str(data_dir),
    ]
    if dashboard:
        cmd.append("--dashboard")
    data_dir.mkdir(parents=True, exist_ok=True)
    stderr_log = open(data_dir / "child.stderr.log", "ab")
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=stderr_log, **kwargs
    )


def _stop_child(proc: subprocess.Popen) -> None:
    import signal

    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait()


def _wait_for_stats(port: int, timeout_s: float = 30.0) -> None:
    url = f"http://127.0.0.1:{port}/stats.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"dashboard never served {url} within {timeout_s}s")


def _mjpeg_client(url: str, stop: threading.Event) -> None:
    """One live viewer: a streaming GET that keeps consuming frames."""
    while not stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                while not stop.is_set():
                    if not resp.read(8192):
                        break
        except OSError:
            time.sleep(0.5)


def _sample(
    pids: dict[str, int], seconds: float, interval_s: float
) -> dict[str, list[float]]:
    import psutil  # [dev] extra — measurement tools only

    procs = {label: psutil.Process(pid) for label, pid in pids.items()}
    for p in procs.values():
        p.cpu_percent(None)  # prime the delta-based meter
    samples: dict[str, list[float]] = {label: [] for label in procs}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(interval_s)
        for label, p in procs.items():
            try:
                samples[label].append(p.cpu_percent(None))
            except psutil.NoSuchProcess:
                raise RuntimeError(
                    f"{label} (pid {p.pid}) died during sampling — see its "
                    "child.stderr.log"
                ) from None
    return samples


def run_baseline(
    clip: Path, base: Path, seconds: float, interval_s: float
) -> list[CpuSummary]:
    cfg_path = base / "baseline.yaml"
    _write_child_config(cfg_path, port=None)
    child = _spawn_replay(clip, cfg_path, base / "baseline", dashboard=False)
    try:
        samples = _sample({"replay (1 cam)": child.pid}, seconds, interval_s)
    finally:
        _stop_child(child)
    return [summarize(k, v) for k, v in samples.items()]


def run_station(
    clip: Path, base: Path, seconds: float, interval_s: float
) -> list[CpuSummary]:
    port = _free_port()
    cfg_a = base / "stationA.yaml"
    cfg_b = base / "stationB.yaml"
    _write_child_config(cfg_a, port=port)
    _write_child_config(cfg_b, port=None)
    child_a = _spawn_replay(clip, cfg_a, base / "stationA", dashboard=True)
    child_b = _spawn_replay(clip, cfg_b, base / "stationB", dashboard=False)
    stop = threading.Event()
    viewer = threading.Thread(
        target=_mjpeg_client,
        args=(f"http://127.0.0.1:{port}/live/video0", stop),
        daemon=True,
    )
    try:
        _wait_for_stats(port)
        viewer.start()
        samples = _sample(
            {
                "replay A (dashboard + MJPEG viewer)": child_a.pid,
                "replay B": child_b.pid,
            },
            seconds,
            interval_s,
        )
    finally:
        stop.set()
        _stop_child(child_a)
        _stop_child(child_b)
    summaries = [summarize(k, v) for k, v in samples.items()]
    summaries.append(
        summarize("station total (A+B)", sum_per_sample(list(samples.values())))
    )
    return summaries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scenario", choices=("baseline", "station", "both"), default="both"
    )
    ap.add_argument(
        "--seconds", type=float, default=300.0, help="sampling window per scenario"
    )
    ap.add_argument("--interval-s", type=float, default=1.0)
    ap.add_argument("--config", type=Path, default=None, help="YAML config path")
    ap.add_argument("--data-dir", type=Path, default=Path("data/cpu"))
    ap.add_argument("--clip", type=Path, default=None, help="reuse a burst clip")
    ap.add_argument("--clip-passes", type=int, default=60)
    ap.add_argument("--out", type=Path, default=None, help="markdown report path")
    args = ap.parse_args(argv)

    base = args.data_dir
    base.mkdir(parents=True, exist_ok=True)
    clip = args.clip
    if clip is None:
        clip = base / "burst_clip.avi"
        cfg = burst_config(load_config(args.config), args.clip_passes)
        print(f"recording {args.clip_passes}-pass burst clip to {clip} ...")
        record_synthetic_clip(cfg, clip)

    sections = [
        "## CPU measurement (spec §11)",
        "",
        f"- method: D10 — burst clip ({BURST_IDLE_S[0]}-{BURST_IDLE_S[1]} s "
        "idle gaps, ~50 passes/min ≈ 7x the 7/min average) replayed at "
        "--speed 1.0 --loop 0; children sampled via psutil at "
        f"{1 / args.interval_s:.0f} Hz for {args.seconds:.0f} s",
        f"- host: {sys.platform}, "
        f"{__import__('os').cpu_count()} logical cores",
        "",
    ]
    if args.scenario in ("baseline", "both"):
        print(f"scenario baseline: sampling {args.seconds:.0f}s ...")
        sections.append(
            render_report(
                "baseline — 1 replay child, no dashboard",
                run_baseline(clip, base, args.seconds, args.interval_s),
            )
        )
    if args.scenario in ("station", "both"):
        print(f"scenario station: sampling {args.seconds:.0f}s ...")
        sections.append(
            render_report(
                "station — 2 replay children + dashboard + 1 MJPEG viewer",
                run_station(clip, base, args.seconds, args.interval_s),
            )
        )
    report = "\n".join(sections)
    out = args.out if args.out is not None else base / "cpu_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nreport written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
