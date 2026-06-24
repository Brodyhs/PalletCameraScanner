r"""STRUCTURED single-variable grid sweep for the injection test harness.

tools/inject_run.py draws speed / distance / angle / contrast / occlusion from
WIDE ranges every pass, so confounds average out and you can never see a clean
per-variable read-rate curve. This tool does the opposite: it pins EVERY axis to
a known-good DECODABLE baseline and varies exactly ONE axis across a fixed grid
of points, so each run yields one clean (ideally monotone) read-rate curve for
that variable.

Each grid point runs --passes-per-point injected pallets through a fresh
CameraInjectionSource + the REAL pipeline, reconciles truth -> PassEvents the
same way inject_run.py does (frame-overlap accounting for the live-camera ts
caveat), and records read-rate + TTFD p50 + n + mean blur_modules. A table prints
to the console and a tidy CSV (one row per point) lands in
data/inject_grid_<var>.csv for plotting / regression.

EFFICIENCY: the live camera is opened ONCE and shared (via inner=) across every
grid point, so the device is not re-enumerated N times — each point still gets a
fresh injection plan / truth.

HONESTY (same caveat as the rest of the harness): injected codes are composited
(modeled optics), so this measures DECODE + PIPELINE under that model, not the
literal optical capture. Pair with record-then-replay for the lens.

Run (stop tools/bench.py / the dashboard first — they hold the camera):
  .\.venv\Scripts\python.exe tools\inject_grid.py --var speed --camera cam-color
  .\.venv\Scripts\python.exe tools\inject_grid.py --var px_per_module --points 10
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from palletscan.app import PipelineRunner  # noqa: E402
from palletscan.config import AppConfig, SyntheticConfig, apply_overrides, load_config  # noqa: E402
from palletscan.sources.base import FrameSource  # noqa: E402
from palletscan.sources.camera import build_camera_source  # noqa: E402
from palletscan.sources.inject import CameraInjectionSource  # noqa: E402
from palletscan.types import Frame, Symbology  # noqa: E402

# --- DECODABLE BASELINE ------------------------------------------------------
# Every SyntheticConfig range collapsed to a degenerate (v, v) single value at a
# known-good operating point: a slow, large, head-on, full-contrast, noise-free,
# unoccluded code. These are consistent with the decodable envelope used by
# tools/inject_dashboard.py and the Phase-A checks (inject_smoke / inject_pipeline
# _check) — they sit at the easy end of each axis so that varying ONE axis is the
# only thing that can break a decode.
_BASELINE: dict = {
    "fps": 55.0,  # match the camera so the truth frame->time mapping lines up
    "speed_mph_range": (1.5, 1.5),
    "px_per_module_range": (6.0, 6.0),
    "angle_deg_range": (0.0, 0.0),
    "contrast_range": (1.0, 1.0),
    "noise_sigma_range": (0.0, 0.0),
    "occlusion_max_frac": 0.0,
    "idle_s_range": (0.2, 0.2),
    "directions": ["right"],
    "max_concurrent": 1,
    "symbologies": [Symbology.QR, Symbology.DATAMATRIX],
}

# --- PER-VARIABLE GRID ENVELOPES ---------------------------------------------
# (lo, hi) the linspace spans, and which SyntheticConfig range field the value
# collapses into. ``exposure`` is special: it is NOT a config field — it is the
# shutter handed to CameraInjectionSource(exposure_s=...) — so its field is None
# and it is applied via the source constructor instead.
_VARS: dict[str, dict] = {
    "speed": {"field": "speed_mph_range", "lo": 0.5, "hi": 14.0},
    "px_per_module": {"field": "px_per_module_range", "lo": 2.0, "hi": 8.0},
    "angle": {"field": "angle_deg_range", "lo": 0.0, "hi": 40.0},
    "contrast": {"field": "contrast_range", "lo": 0.3, "hi": 1.0},
    "occlusion": {"field": "occlusion_max_frac", "lo": 0.0, "hi": 0.3},
    "exposure": {"field": None, "lo": 1.0, "hi": 10.0},  # ms; via exposure_s
}


def baseline_synthetic(app_cfg: AppConfig, *, num_passes: int, seed: int) -> SyntheticConfig:
    """The decodable baseline SyntheticConfig: start from the loaded config and
    collapse every range to its degenerate single-value tuple."""
    return app_cfg.synthetic.model_copy(
        update={**_BASELINE, "num_passes": num_passes, "seed": seed}
    )


def point_synthetic(base: SyntheticConfig, var: str, value: float) -> SyntheticConfig:
    """The baseline with ONLY ``var``'s axis collapsed to (value, value).

    ``exposure`` does not live in the config (it is the source shutter), so for
    that variable the returned config is the unmodified baseline and the caller
    applies the value via ``exposure_s``."""
    field = _VARS[var]["field"]
    if field is None:  # exposure: handled by the source, not the config
        return base
    if field == "occlusion_max_frac":  # a scalar, not a (lo, hi) range
        return base.model_copy(update={field: float(value)})
    return base.model_copy(update={field: (float(value), float(value))})


def grid_values(var: str, points: int) -> list[float]:
    spec = _VARS[var]
    return [float(v) for v in np.linspace(spec["lo"], spec["hi"], points)]


class _SharedLiveInner(FrameSource):
    """Re-iterable, close-guarded wrapper around ONE already-open live source.

    The point of the harness is to open the camera once and reuse it across all
    grid points. But each grid point drives its own CameraInjectionSource +
    PipelineRunner, and the runner's source thread calls ``source.close()`` in
    its finally — which, for CameraInjectionSource, propagates to
    ``inner.close()``. If ``inner`` were the real device that would tear it down
    after the FIRST point.

    So this adapter makes ``close()`` a no-op (the shared device survives every
    point) and exposes ``shutdown()`` for the real teardown at the very end. Its
    ``frames()`` delegates to the wrapped source, which yields a fresh live
    stream each call (points run strictly sequentially, so only one consumer
    reads the open device at a time). It deliberately does NOT wrap the watchdog
    (whose frames() is single-use); it wraps the bare CameraSource, which is
    re-iterable while the device stays open."""

    def __init__(self, wrapped: FrameSource) -> None:
        self._wrapped = wrapped

    @property
    def source_id(self) -> str:
        return self._wrapped.source_id

    @property
    def nominal_fps(self) -> float | None:
        return self._wrapped.nominal_fps

    @property
    def live(self) -> bool:
        return True

    @property
    def epoch_wall(self) -> float | None:
        wall = getattr(self._wrapped, "epoch_wall", None)
        return float(wall) if wall is not None else None

    def reopen(self) -> None:  # keep it Reopenable in case a wrapper expects it
        reopen = getattr(self._wrapped, "reopen", None)
        if reopen is not None:
            reopen()

    def frames(self):
        return self._wrapped.frames()

    def close(self) -> None:
        """No-op: per-point sources must NOT close the shared device."""

    def shutdown(self) -> None:
        """Really release the shared device (called once, at the end)."""
        self._wrapped.close()


def _reconcile(runner: PipelineRunner, src: CameraInjectionSource) -> tuple[int, int, int, list[float]]:
    """Join truth -> events EXACTLY as tools/inject_run.py does.

    Returns (decoded, missed, unaccounted, ttfd_ms_list). Accounting is by FRAME
    overlap, not time: a live camera's event ts is monotonic capture time (not
    frame_index/fps), so reconcile_truth's time-window match misaligns. This
    mirrors inject_run.py's _ov frame-overlap accounting verbatim."""
    events = getattr(runner, "collected_events", [])
    passes = {ev.payload: ev for ev in events if getattr(ev, "kind", None) == "pass"}
    ttfds = [
        (passes[rec.payload].first_decode_ts - passes[rec.payload].first_seen_ts) * 1000.0
        for rec in src.truth
        if rec.payload in passes and passes[rec.payload].first_decode_ts is not None
    ]
    miss_spans = [
        (ev.first_frame, ev.last_frame)
        for ev in events
        if getattr(ev, "kind", None) == "miss"
    ]

    def _ov(a0, a1, b0, b1):
        return a0 <= b1 and b0 <= a1

    decoded = missed = unacc = 0
    for r in src.truth:
        if r.payload in passes:
            decoded += 1
        elif any(_ov(r.first_frame, r.last_frame, m0, m1) for m0, m1 in miss_spans):
            missed += 1
        else:
            unacc += 1
    return decoded, missed, unacc, ttfds


def run_point(
    app_cfg: AppConfig,
    syn: SyntheticConfig,
    *,
    inner: FrameSource,
    exposure_ms: float,
) -> dict:
    """Run one grid point through a fresh CameraInjectionSource + PipelineRunner
    against the SHARED inner camera, and return its reconciled metrics."""
    cfg = app_cfg.model_copy(update={"synthetic": syn})
    src = CameraInjectionSource(
        syn, cfg, exposure_s=exposure_ms / 1000.0, inner=inner
    )
    runner = PipelineRunner.from_config(cfg, source=src)
    runner.run()
    decoded, missed, unacc, ttfds = _reconcile(runner, src)
    n = len(src.truth)
    blur = [r.params.get("blur_modules", float("nan")) for r in src.truth]
    mean_blur = statistics.fmean(blur) if blur else float("nan")
    return {
        "n": n,
        "decoded": decoded,
        "missed": missed,
        "unaccounted": unacc,
        "read_rate": (100.0 * decoded / n) if n else 0.0,
        "ttfd_p50_ms": statistics.median(ttfds) if ttfds else None,
        "mean_blur_modules": mean_blur,
    }


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="Single-variable grid sweep for the injection harness "
        "(one clean read-rate curve per run)."
    )
    ap.add_argument("--config", default="config/station.yaml")
    ap.add_argument("--camera", default="cam-color",
                    help="cameras[].id to inject onto")
    ap.add_argument("--var", required=True, choices=sorted(_VARS),
                    help="the single axis to sweep; everything else is pinned to "
                         "the decodable baseline")
    ap.add_argument("--points", type=int, default=8,
                    help="number of grid points across the variable's envelope")
    ap.add_argument("--passes-per-point", type=int, default=40,
                    help="injected passes run at each grid point")
    ap.add_argument("--exposure-ms", type=float, default=2.0,
                    help="baseline modeled shutter (the held value for every axis "
                         "EXCEPT --var exposure, which sweeps it)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--data-dir", default="data",
                    help="rebase sink/evidence/log/lock paths under this dir")
    a = ap.parse_args()

    if a.points < 2:
        print("--points must be >= 2 to form a grid", file=sys.stderr, flush=True)
        return 2
    if a.passes_per_point < 1:
        print("--passes-per-point must be >= 1", file=sys.stderr, flush=True)
        return 2

    app_cfg = load_config(a.config)
    app_cfg = app_cfg.model_copy(
        update={"source": app_cfg.source.model_copy(update={"camera": a.camera})}
    )
    app_cfg = apply_overrides(app_cfg, data_dir=a.data_dir)

    values = grid_values(a.var, a.points)
    base = baseline_synthetic(app_cfg, num_passes=a.passes_per_point, seed=a.seed)

    print(f"grid sweep: var={a.var}  points={a.points}  "
          f"passes/point={a.passes_per_point}  camera={a.camera}", flush=True)
    print(f"  baseline shutter {a.exposure_ms} ms; varying {a.var} over "
          f"{values[0]:g}..{values[-1]:g}", flush=True)
    print("  (everything else pinned to the decodable baseline)\n", flush=True)

    # Open the live camera ONCE and share it across all grid points. The bare
    # camera source (re-iterable while open) is wrapped in a close-guarded
    # adapter so each per-point CameraInjectionSource.close() can't tear down the
    # shared device — only shutdown() at the end does.
    shared = _SharedLiveInner(build_camera_source(app_cfg))
    rows: list[dict] = []
    try:
        for i, value in enumerate(values):
            syn = point_synthetic(base, a.var, value)
            # Re-seed per point so each point gets a fresh, distinct injection
            # plan/truth (still deterministic given --seed).
            syn = syn.model_copy(update={"seed": a.seed + i})
            exposure_ms = value if a.var == "exposure" else a.exposure_ms
            m = run_point(app_cfg, syn, inner=shared, exposure_ms=exposure_ms)
            m["value"] = value
            rows.append(m)
            bar = "#" * int(round(m["read_rate"] / 5))
            ttfd = "-" if m["ttfd_p50_ms"] is None else f"{m['ttfd_p50_ms']:.0f}"
            print(f"  {a.var}={value:7.3f} | {m['read_rate']:5.0f}% "
                  f"(n={m['n']:<3}) TTFD {ttfd:>4}ms  blur~{m['mean_blur_modules']:.2f}  {bar}",
                  flush=True)
    finally:
        shared.shutdown()

    # --- report ---------------------------------------------------------------
    print("\n" + "=" * 64)
    print(f"GRID REPORT  var={a.var}  ({a.points} points, {a.passes_per_point} "
          f"passes/point, baseline shutter {a.exposure_ms} ms)")
    print("=" * 64)
    header = f"{a.var:>12} | read% |  n  | TTFD p50 | blur_mod | bar"
    print(header)
    print("-" * len(header))
    for m in rows:
        bar = "#" * int(round(m["read_rate"] / 5))
        ttfd = "  -  " if m["ttfd_p50_ms"] is None else f"{m['ttfd_p50_ms']:5.0f}"
        print(f"{m['value']:12.3f} | {m['read_rate']:4.0f}% | {m['n']:<3} | "
              f"{ttfd} ms | {m['mean_blur_modules']:7.2f}  | {bar}")

    # Verify exact accounting: every point must have n == passes-per-point.
    short = [m for m in rows if m["n"] != a.passes_per_point]
    if short:
        print("\nWARNING: dropped accounting at "
              f"{len(short)} point(s) (n != {a.passes_per_point}): "
              f"{[(m['value'], m['n']) for m in short]}", flush=True)
    else:
        print(f"\naccounting OK: every point has n={a.passes_per_point}", flush=True)

    # --- tidy CSV + JSONL for plotting / regression ---------------------------
    out_dir = Path(a.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = ["var", "value", "read_rate", "n", "decoded", "missed",
            "unaccounted", "ttfd_p50_ms", "mean_blur_modules"]
    csv_path = out_dir / f"inject_grid_{a.var}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for m in rows:
            w.writerow({
                "var": a.var,
                "value": m["value"],
                "read_rate": m["read_rate"],
                "n": m["n"],
                "decoded": m["decoded"],
                "missed": m["missed"],
                "unaccounted": m["unaccounted"],
                "ttfd_p50_ms": m["ttfd_p50_ms"],
                "mean_blur_modules": m["mean_blur_modules"],
            })
    jsonl_path = out_dir / f"inject_grid_{a.var}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for m in rows:
            f.write(json.dumps({"var": a.var, **m}) + "\n")

    print(f"\nwrote {csv_path}  and  {jsonl_path}", flush=True)
    print("NOTE: injected codes are composited (modeled optics) — relative "
          "signal, not a lens measurement.", flush=True)
    return 0 if not short else 1


# --- camera-free structural self-check ---------------------------------------
def _self_check() -> int:
    """Build the baseline + per-point configs for EVERY var WITHOUT a camera and
    assert that only the chosen axis changes between points (the collapse logic).
    Prints PASS/FAIL. Camera-free: constructs SyntheticConfig directly."""
    app_cfg = AppConfig()  # full defaults; no camera, no device access
    base = baseline_synthetic(app_cfg, num_passes=4, seed=7)

    # The range/scalar fields a per-point copy could possibly touch.
    watched = [
        "speed_mph_range", "px_per_module_range", "angle_deg_range",
        "contrast_range", "noise_sigma_range", "occlusion_max_frac",
        "idle_s_range",
    ]
    ok = True
    for var in sorted(_VARS):
        field = _VARS[var]["field"]
        values = grid_values(var, 4)
        configs = [point_synthetic(base, var, v) for v in values]

        if field is None:  # exposure: config must be IDENTICAL across points
            identical = all(
                c.model_dump() == base.model_dump() for c in configs
            )
            status = "PASS" if identical else "FAIL"
            ok = ok and identical
            print(f"  [{status}] {var:<13}: config unchanged across points "
                  f"(swept via exposure_s, not config)")
            continue

        # For every OTHER watched field, the value must be constant across the
        # whole grid (== the baseline); ONLY ``field`` may vary.
        changed_only_target = True
        for other in watched:
            base_val = getattr(base, other)
            across = {getattr(c, other) for c in configs}
            if other == field:
                # The target axis must actually take >1 distinct value.
                if len(across) <= 1:
                    changed_only_target = False
            else:
                if across != {base_val}:
                    changed_only_target = False
        status = "PASS" if changed_only_target else "FAIL"
        ok = ok and changed_only_target
        print(f"  [{status}] {var:<13}: only {field} varies across the grid")

    print(f"\nCONFIG-COLLAPSE SELF-CHECK: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(_self_check())
    raise SystemExit(main())
