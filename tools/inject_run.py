r"""Condition-sweep harness: inject many physics-degraded synthetic pallets across
the full envelope onto the LIVE feed, run them through the REAL pipeline, and
print a per-condition read-rate + TTFD report so you can see exactly where it
breaks before the field.

Each pass draws speed / distance(px-per-module) / angle / contrast / occlusion /
symbology from wide ranges; blur is modeled from --exposure-ms (the field shutter
you intend to run, NOT necessarily the rig's current exposure). [PASS]/[MISS]
stream live to the console; the per-condition breakdown prints at the end.

BLUR-STRESS SWEEP: --exposure-ms now takes a COMMA LIST (default "1,2,4,8"). Blur
scales linearly with exposure (blur_px = speed_mps * exposure_s * px_per_meter in
CameraInjectionSource._plan_pass), so sweeping several shutters fills the heavy-
blur bins that a single 1 ms pass leaves empty, resolving the read-rate-vs-blur
cliff. The stress sweep also raises the speed ceiling and restricts travel to the
HORIZONTAL axis (right/left) because render.motion_blur is horizontal-only -- a
vertical pass would land a clean code in a heavy blur_modules bin and pollute it.

HONESTY: injected codes are composited (modeled optics), so this measures the
DECODE + PIPELINE under that model -- pair with record-then-replay for the lens.

Run (stop tools/bench.py first):
  .\.venv\Scripts\python.exe tools\inject_run.py --passes 200 --exposure-ms 1,2,4,8
  .\.venv\Scripts\python.exe tools\inject_run.py --passes 80 --exposure-ms 16   # single shutter, legacy behavior
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# blur_modules bin edges -- refined so the cliff resolves: the old
# [0,0.5,1,2,5,100] lumped everything >=5 modules into one bin, hiding where
# decode actually dies. The 2/3/5/8 splits expose the knee.
BLUR_EDGES = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 100.0]

# Stress-sweep envelope: raise the speed ceiling (more px/frame -> more blur at a
# given shutter) so the heavy bins fill, and restrict to horizontal travel so the
# horizontal-only motion-blur model stays honest (see module docstring).
STRESS_SPEED_MPH_RANGE = [0.5, 16.0]
STRESS_DIRECTIONS = ["right", "left"]

# Gate thresholds.
HEAVY_BLUR_MIN = 3.0      # "heavy" means blur_modules >= this
HEAVY_BIN_MIN_N = 20      # at least one heavy bin must hold >= this many samples
MONOTONE_TOL_PP = 3.0     # read-rate may not RISE by more than this (pp) per step


def _parse_exposures(s: str) -> list[float]:
    """Parse a comma list of exposures (ms) into a list[float]. A single value
    like "4" yields [4.0] and behaves exactly like the old single-float arg."""
    vals = [float(tok) for tok in str(s).split(",") if tok.strip() != ""]
    if not vals:
        raise argparse.ArgumentTypeError(f"--exposure-ms: no values parsed from {s!r}")
    if any(v <= 0 for v in vals):
        raise argparse.ArgumentTypeError(f"--exposure-ms: values must be > 0 (got {vals})")
    return vals


def _bucket(rows, key, edges):
    """rows: list of (decoded: bool, params: dict). Print read-rate per bin."""
    labels, rates = [], []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        b = [d for d, p in rows if lo <= p.get(key, -1e9) < hi or (i == len(edges) - 2 and p.get(key) == hi)]
        rate = 100.0 * sum(b) / len(b) if b else None
        labels.append(f"{lo:g}-{hi:g}")
        rates.append((rate, len(b)))
    print(f"  by {key}:")
    for lab, (rate, n) in zip(labels, rates):
        bar = "" if rate is None else "#" * int(round(rate / 5))
        rt = "  -  " if rate is None else f"{rate:5.0f}%"
        print(f"    {lab:>10} | {rt} (n={n:<3}) {bar}")


def _blur_bin_stats(rows, edges=BLUR_EDGES):
    """rows: list of (decoded: bool, params: dict). Return list of dicts with the
    bin label, edge lo/hi, sample count n, decoded count, and read-rate (or None
    when the bin is empty), bucketed by blur_modules using the same edge semantics
    as _bucket (last bin is closed on the right)."""
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        b = [d for d, p in rows
             if lo <= p.get("blur_modules", -1e9) < hi
             or (i == len(edges) - 2 and p.get("blur_modules") == hi)]
        n = len(b)
        dec = sum(1 for d in b if d)
        out.append({
            "label": f"{lo:g}-{hi:g}",
            "lo": lo, "hi": hi, "n": n, "decoded": dec,
            "rate": (100.0 * dec / n) if n else None,
        })
    return out


def _gate(per_exposure, *, heavy_blur_min=HEAVY_BLUR_MIN, heavy_bin_min_n=HEAVY_BIN_MIN_N,
          monotone_tol_pp=MONOTONE_TOL_PP, blur_edges=BLUR_EDGES):
    """Decide BLUR-STRESS PASS/FAIL.

    per_exposure: ordered list (by increasing exposure_ms) of dicts:
        {"exposure_ms": float, "rows": [(decoded: bool, params: dict), ...]}

    Returns (passed: bool, reasons: list[str]). Two conditions, BOTH required:
      (a) HEAVY BIN POPULATED -- pooling all exposures, at least one blur_modules
          bin whose lower edge is >= heavy_blur_min holds >= heavy_bin_min_n
          samples (proves the original empty-heavy-bin gap is closed).
      (b) MONOTONE NON-INCREASING -- overall read-rate, ordered by increasing
          exposure, never RISES by more than monotone_tol_pp percentage points
          step-to-step (more blur should not read BETTER; small noise tolerated)."""
    reasons = []

    # (a) heavy bin populated -- pool every exposure's rows.
    pooled = [r for e in per_exposure for r in e["rows"]]
    bins = _blur_bin_stats(pooled, blur_edges)
    heavy = [b for b in bins if b["lo"] >= heavy_blur_min]
    heavy_ok = any(b["n"] >= heavy_bin_min_n for b in heavy)
    if heavy_ok:
        best = max((b for b in heavy), key=lambda b: b["n"], default=None)
        reasons.append(
            f"PASS heavy-bin: blur_modules>={heavy_blur_min:g} bin "
            f"'{best['label']}' has n={best['n']} (>= {heavy_bin_min_n})"
        )
    else:
        got = max((b["n"] for b in heavy), default=0)
        reasons.append(
            f"FAIL heavy-bin: no blur_modules>={heavy_blur_min:g} bin reached "
            f"n>={heavy_bin_min_n} (best n={got}); heavy bins never filled"
        )

    # (b) monotone non-increasing within tolerance, ordered by exposure.
    ordered = sorted(per_exposure, key=lambda e: e["exposure_ms"])
    overalls = []
    for e in ordered:
        rows = e["rows"]
        rate = (100.0 * sum(1 for d, _ in rows if d) / len(rows)) if rows else None
        overalls.append((e["exposure_ms"], rate))
    mono_ok = True
    worst_rise = 0.0
    worst_step = None
    for (ms0, r0), (ms1, r1) in zip(overalls, overalls[1:]):
        if r0 is None or r1 is None:
            continue
        rise = r1 - r0
        if rise > worst_rise:
            worst_rise = rise
            worst_step = (ms0, r0, ms1, r1)
        if rise > monotone_tol_pp:
            mono_ok = False
    if len(overalls) < 2:
        reasons.append("PASS monotone: single exposure (nothing to compare)")
    elif mono_ok:
        if worst_step is None:
            reasons.append("PASS monotone: read-rate non-increasing across exposures")
        else:
            ms0, r0, ms1, r1 = worst_step
            reasons.append(
                f"PASS monotone: largest rise {worst_rise:.1f}pp "
                f"({ms0:g}ms {r0:.1f}% -> {ms1:g}ms {r1:.1f}%) <= {monotone_tol_pp:g}pp tol"
            )
    else:
        ms0, r0, ms1, r1 = worst_step
        reasons.append(
            f"FAIL monotone: read-rate ROSE {worst_rise:.1f}pp "
            f"({ms0:g}ms {r0:.1f}% -> {ms1:g}ms {r1:.1f}%) > {monotone_tol_pp:g}pp tol; "
            f"more blur should not read better"
        )

    return (heavy_ok and mono_ok), reasons


def _build_syn(app_cfg, *, passes, seed):
    """Build the stress-sweep SyntheticConfig (envelope shared across exposures).

    Speed ceiling raised and travel restricted to the horizontal axis so the heavy
    blur_modules bins actually fill with codes whose smear is along their motion
    (render.motion_blur is horizontal-only). px_per_module / angle / contrast are
    left as-is so blur is the moving axis of this sweep."""
    from palletscan.types import Symbology  # local import keeps --help cheap
    return app_cfg.synthetic.model_copy(
        update={
            "num_passes": passes,
            "fps": 55.0,
            "seed": seed,
            "speed_mph_range": STRESS_SPEED_MPH_RANGE,
            "directions": list(STRESS_DIRECTIONS),
            "px_per_module_range": [2.0, 8.0],
            "angle_deg_range": [0.0, 35.0],
            "contrast_range": [0.45, 1.0],
            "noise_sigma_range": [0.0, 6.0],
            "occlusion_max_frac": 0.15,
            "idle_s_range": [0.3, 0.6],
            "symbologies": [Symbology.QR, Symbology.DATAMATRIX],
        }
    )


def _exposure_tag(exposure_ms: float) -> str:
    """Filesystem-safe tag for per-exposure output files (e.g. 4.0 -> '4', 2.5 -> '2p5')."""
    s = f"{exposure_ms:g}".replace(".", "p")
    return s


def _account(truth, events, nominal_fps):
    """Split truth passes into (decoded, missed, unaccounted) counts.

    Truth ``first_frame``/``last_frame`` are nominal-fps ticks of the live
    ts clock (TRUTH TIME-BASE in palletscan/sources/inject.py) — the same
    axis live Pass/MissEvent timestamps use — so the app's own ts-space
    ``reconcile_truth`` is the correct join. The old frame-index overlap
    matched those ticks against MissEvent camera frame indices, which
    diverge from ts under watchdog outages and real-vs-nominal fps error:
    every genuinely missed pass then reported "not-flagged" — the exact
    mis-accounting the truth time-base fix eliminated (re-review of
    REVIEW_bringup_4d95b67)."""
    from palletscan.app import reconcile_truth

    rec = reconcile_truth(truth, events, nominal_fps)
    return rec.decoded, rec.missed, len(rec.unaccounted)


def _run_one_exposure(app_cfg, syn, *, exposure_ms, passes, dashboard_flag, write_truth=True):
    """Build a FRESH CameraInjectionSource + PipelineRunner for this exposure (the
    source is single-use), run it, print the per-condition report, and return
    {"exposure_ms", "rows", "overall"}. Per-exposure file tagging prevents clobber."""
    from palletscan.app import PipelineRunner
    from palletscan.sources.inject import CameraInjectionSource

    src = CameraInjectionSource(syn, app_cfg, exposure_s=exposure_ms / 1000.0)
    print(f"sweep: {passes} passes, modeled shutter {exposure_ms:g} ms, real feed "
          f"@ {src.nominal_fps} fps. Streaming [PASS]/[MISS]...\n", flush=True)

    runner = PipelineRunner.from_config(app_cfg, source=src)
    dashboard = None
    if dashboard_flag:
        from palletscan.cli import _DashboardUnavailable, _start_dashboard
        try:
            dashboard = _start_dashboard(app_cfg, {runner.source.source_id: runner}, None)
            print("  ^^ open that URL in a browser to WATCH the injected pallets live ^^\n",
                  flush=True)
        except _DashboardUnavailable as exc:
            print(f"dashboard unavailable: {exc}", file=sys.stderr, flush=True)
    try:
        runner.run()
    finally:
        if dashboard is not None:
            dashboard.stop()

    # join truth -> events for per-condition read-rate + TTFD
    events = getattr(runner, "collected_events", [])
    passes_map = {ev.payload: ev for ev in events
                  if getattr(ev, "kind", None) == "pass"}
    rows = [(rec.payload in passes_map, rec.params) for rec in src.truth]
    ttfds = [(passes_map[rec.payload].first_decode_ts - passes_map[rec.payload].first_seen_ts) * 1000.0
             for rec in src.truth
             if rec.payload in passes_map
             and passes_map[rec.payload].first_decode_ts is not None]

    # ts-space accounting: truth spans are nominal-fps ticks of the live ts
    # clock, the same axis Pass/MissEvent timestamps use (see _account).
    decoded, missed, unacc = _account(src.truth, events, src.nominal_fps)

    tag = _exposure_tag(exposure_ms)
    if write_truth:
        src.write_truth_jsonl(f"data/inject_truth_exp{tag}ms.jsonl")
        _write_rows_csv(rows, f"data/inject_rows_exp{tag}ms.csv", exposure_ms)
    overall = 100.0 * decoded / len(rows) if rows else 0.0

    print("\n" + "=" * 60)
    print(f"SWEEP REPORT  ({len(rows)} passes, shutter {exposure_ms:g} ms modeled)")
    print("=" * 60)
    print(f"overall READ RATE : {overall:.1f}%   ({decoded}/{len(rows)} read)")
    print(f"  not read: {len(rows) - decoded}  =  {missed} flagged-miss + {unacc} not-flagged")
    if unacc:
        print("  (not-flagged: injected non-decodes the pipeline didn't miss-flag -- usually the")
        print("   motion ROI swept over a REAL code in-frame and decoded that instead. The READ")
        print("   RATE is robust regardless; clear real codes for pristine miss accounting.)")
    if ttfds:
        print(f"time-to-first-decode: p50 {statistics.median(ttfds):.0f} ms / "
              f"p95 {statistics.quantiles(ttfds, n=20)[18] if len(ttfds) >= 20 else max(ttfds):.0f} ms")
    print()
    _bucket(rows, "blur_modules", BLUR_EDGES)
    _bucket(rows, "speed_mph", [0.5, 2, 4, 6, 8, 12, 16.01])
    _bucket(rows, "px_per_module", [2, 3, 4, 5, 6, 8.01])
    _bucket(rows, "angle_deg", [0, 10, 20, 35.01])
    _bucket(rows, "contrast", [0.45, 0.6, 0.75, 1.01])
    _bucket(rows, "occlusion_frac", [0, 0.05, 0.1, 0.16])
    if write_truth:
        print(f"\ntruth written -> data/inject_truth_exp{tag}ms.jsonl  "
              f"(join to events for deeper analysis)")
    print("NOTE: injected codes are composited (modeled optics) -- relative signal, "
          "not a lens measurement.")
    return {"exposure_ms": exposure_ms, "rows": rows, "overall": overall}


def _write_rows_csv(rows, path, exposure_ms):
    """Write per-pass rows to CSV (one file per exposure to avoid clobber)."""
    keys = ["exposure_ms", "decoded", "blur_modules", "blur_px", "speed_mph",
            "px_per_module", "angle_deg", "contrast", "noise_sigma",
            "occlusion_frac", "direction", "modules"]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for decoded, params in rows:
            row = {"exposure_ms": exposure_ms, "decoded": int(bool(decoded))}
            for k in keys[2:]:
                row[k] = params.get(k)
            w.writerow(row)


def _print_overall_summary(per_exposure):
    """Print the cross-exposure summary: read-rate per exposure, and read-rate per
    blur_modules bin pooled across all exposures."""
    print("\n" + "#" * 60)
    print("OVERALL BLUR-STRESS SUMMARY")
    print("#" * 60)
    print("read-rate per exposure (increasing shutter -> increasing blur):")
    for e in sorted(per_exposure, key=lambda e: e["exposure_ms"]):
        rows = e["rows"]
        n = len(rows)
        dec = sum(1 for d, _ in rows if d)
        rate = (100.0 * dec / n) if n else 0.0
        bar = "#" * int(round(rate / 5))
        print(f"    {e['exposure_ms']:>5g} ms | {rate:5.1f}% (n={n:<4}, read={dec}) {bar}")

    pooled = [r for e in per_exposure for r in e["rows"]]
    print("\nread-rate per blur_modules bin (pooled across all exposures):")
    for b in _blur_bin_stats(pooled, BLUR_EDGES):
        if b["rate"] is None:
            rt, bar = "  -  ", ""
        else:
            rt = f"{b['rate']:5.0f}%"
            bar = "#" * int(round(b["rate"] / 5))
        heavy = "  <-- heavy" if b["lo"] >= HEAVY_BLUR_MIN else ""
        print(f"    {b['label']:>10} | {rt} (n={b['n']:<4}, read={b['decoded']}) {bar}{heavy}")


def _self_check() -> int:
    """Camera-free self-check of the parse helper + the bin/gate logic against
    fabricated rows. Prints PASS/FAIL lines and returns 0 iff every check passes."""
    ok = True

    def _expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1) parse helper
    _expect("parse '1,2,4,8' -> [1.0,2.0,4.0,8.0]",
            _parse_exposures("1,2,4,8") == [1.0, 2.0, 4.0, 8.0])
    _expect("parse single '4' -> [4.0]", _parse_exposures("4") == [4.0])
    _expect("parse ' 2 , 4 ' (whitespace) -> [2.0,4.0]",
            _parse_exposures(" 2 , 4 ") == [2.0, 4.0])
    _expect("parse trailing comma '1,2,' -> [1.0,2.0]",
            _parse_exposures("1,2,") == [1.0, 2.0])
    bad = False
    try:
        _parse_exposures("0,1")
    except argparse.ArgumentTypeError:
        bad = True
    _expect("parse '0,1' rejects non-positive", bad)

    # 2) bin edges sanity
    _expect("BLUR_EDGES refined to include 2/3/5/8 splits",
            BLUR_EDGES == [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 100.0])
    # a heavy code at blur_modules=4 must land in the '3-5' bin, not the tail
    stats = _blur_bin_stats([(True, {"blur_modules": 4.0})], BLUR_EDGES)
    b35 = next(b for b in stats if b["label"] == "3-5")
    _expect("blur_modules=4.0 buckets into '3-5'", b35["n"] == 1)
    b_tail = next(b for b in stats if b["label"] == "8-100")
    _expect("blur_modules=4.0 NOT in tail '8-100'", b_tail["n"] == 0)

    def _rows(specs):
        # specs: list of (blur_modules, decoded) -> rows of (decoded, params)
        return [(dec, {"blur_modules": bm}) for bm, dec in specs]

    # 3a) clean monotone cliff WITH a populated heavy bin -> PASS
    #     1ms: ~100% read at low blur; 8ms: heavy blur, mostly misses.
    good = [
        {"exposure_ms": 1.0, "rows": _rows([(0.3, True)] * 40)},                       # 100%
        {"exposure_ms": 2.0, "rows": _rows([(0.8, True)] * 35 + [(0.8, False)] * 5)},  # 87.5%
        {"exposure_ms": 4.0, "rows": _rows([(2.0, True)] * 20 + [(2.0, False)] * 20)}, # 50%
        # heavy bin (blur_modules=4 -> '3-5') with 30 samples, mostly miss -> 10%
        {"exposure_ms": 8.0, "rows": _rows([(4.0, True)] * 3 + [(4.0, False)] * 27)},  # 10%
    ]
    passed, reasons = _gate(good)
    print("  -- gate on clean monotone cliff --")
    for r in reasons:
        print(f"      {r}")
    _expect("clean monotone cliff -> gate PASS", passed is True)

    # 3b) RISING read-rate (more blur reads BETTER) -> FAIL on monotone,
    #     even though the heavy bin is populated.
    rising = [
        {"exposure_ms": 1.0, "rows": _rows([(0.3, True)] * 10 + [(0.3, False)] * 30)},  # 25%
        {"exposure_ms": 8.0, "rows": _rows([(4.0, True)] * 36 + [(4.0, False)] * 4)},   # 90%
    ]
    passed2, reasons2 = _gate(rising)
    print("  -- gate on rising (inverted) read-rate --")
    for r in reasons2:
        print(f"      {r}")
    _expect("rising read-rate -> gate FAIL", passed2 is False)

    # 3c) monotone but EMPTY heavy bin (the original gap) -> FAIL on heavy-bin.
    empty_heavy = [
        {"exposure_ms": 1.0, "rows": _rows([(0.3, True)] * 40)},                        # 100%
        {"exposure_ms": 2.0, "rows": _rows([(0.9, True)] * 20 + [(0.9, False)] * 20)},  # 50%
    ]
    passed3, reasons3 = _gate(empty_heavy)
    print("  -- gate on monotone-but-empty-heavy-bin --")
    for r in reasons3:
        print(f"      {r}")
    _expect("monotone but no heavy samples -> gate FAIL", passed3 is False)

    print(f"\nSELF-CHECK {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/station.yaml")
    ap.add_argument("--camera", default=None,
                    help="cameras[].id to inject onto (required when >1 camera configured)")
    ap.add_argument("--passes", type=int, default=120)
    ap.add_argument("--exposure-ms", type=_parse_exposures, default=[1.0, 2.0, 4.0, 8.0],
                    help="COMMA LIST of modeled shutters (ms) driving blur, swept in order "
                         "(default '1,2,4,8'). A single value e.g. '4' behaves like before.")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dashboard", action="store_true",
                    help="serve the live web dashboard so you can WATCH the injected pallets "
                         "sweep the feed with decode overlays (uses config web.host:web.port)")
    ap.add_argument("--self-check", action="store_true",
                    help="run the camera-free self-check of the parse + gate logic and exit")
    a = ap.parse_args()

    if a.self_check:
        print("BLUR-STRESS self-check (camera-free):")
        return _self_check()

    from palletscan.config import load_config

    app_cfg = load_config(a.config)
    if a.camera is not None:
        app_cfg = app_cfg.model_copy(
            update={"source": app_cfg.source.model_copy(update={"camera": a.camera})}
        )
    syn = _build_syn(app_cfg, passes=a.passes, seed=a.seed)
    app_cfg = app_cfg.model_copy(update={"synthetic": syn})

    exposures = a.exposure_ms  # already a list[float] via the type= parser
    print(f"BLUR-STRESS sweep over exposures (ms): "
          f"{', '.join(f'{e:g}' for e in exposures)}  "
          f"[speed {STRESS_SPEED_MPH_RANGE[0]:g}-{STRESS_SPEED_MPH_RANGE[1]:g} mph, "
          f"directions {'/'.join(STRESS_DIRECTIONS)}]\n", flush=True)

    per_exposure = []
    for exposure_ms in exposures:
        print("\n" + "*" * 60)
        print(f"* EXPOSURE {exposure_ms:g} ms")
        print("*" * 60)
        result = _run_one_exposure(
            app_cfg, syn,
            exposure_ms=exposure_ms,
            passes=a.passes,
            dashboard_flag=a.dashboard,
        )
        per_exposure.append(result)

    # Cross-exposure summary + exit-code gate.
    _print_overall_summary(per_exposure)
    passed, reasons = _gate(per_exposure)
    print("\nBLUR-STRESS gate:")
    for r in reasons:
        print(f"  - {r}")
    verdict = "PASS" if passed else "FAIL"
    print(f"\nBLUR-STRESS {verdict} "
          f"(heavy bin blur_modules>={HEAVY_BLUR_MIN:g} needs n>={HEAVY_BIN_MIN_N}; "
          f"read-rate must not rise >{MONOTONE_TOL_PP:g}pp per exposure step)")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
