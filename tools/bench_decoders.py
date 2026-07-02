"""Benchmark ThreadPool vs ProcessPool for pyzbar/pylibdmtx decode tasks.

The spec requires measuring rather than assuming: both libraries release the
GIL inside their C calls, so threads *should* win (no ndarray pickling), but
this script verifies it on the actual machine, and stress-checks per-call
thread safety by comparing concurrent results against serial ground truth.

Run:  python tools/bench_decoders.py
"""

from __future__ import annotations

import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.pipeline.decoders import PylibdmtxDecoder, PyzbarDecoder
from palletscan.sources.render import motion_blur, render_datamatrix, render_qr
from palletscan.types import Symbology

TASKS_PER_RUN = 60
WORKER_COUNTS = (1, 2, 4)


def _p95(latencies_ms: list[float]) -> float:
    """Interpolated 95th percentile for any n >= 1.

    ``statistics.quantiles(n=20)`` requires >= 20 samples, and the hard
    workload has only 18 — the old ``len >= 20`` guard silently printed
    max() under a column headed "p95 ms". numpy's linear interpolation is
    defined for any sample count (callers still label small n honestly)."""
    return float(np.percentile(latencies_ms, 95))


def _make_hard_workload() -> list[tuple[str, np.ndarray, Symbology, str]]:
    """Harder QR+DM crops (low pitch x heavier motion blur) to separate a
    robust decoder from a fragile one. Each item carries its true payload."""
    work = []
    for ppm in (2.5, 3.0, 4.0):
        for blur_modules in (0.5, 1.0, 1.5):
            payload = f"PLT-{int(ppm * 100):06d}"
            for rendered in (render_qr(payload, ppm), render_datamatrix(payload, ppm)):
                img = motion_blur(rendered.image, blur_modules * ppm)
                canvas = np.full((img.shape[0] + 80, img.shape[1] + 80), 120, np.uint8)
                canvas[40 : 40 + img.shape[0], 40 : 40 + img.shape[1]] = img
                work.append(
                    (
                        f"{rendered.symbology.value}@{ppm}ppm/{blur_modules}mod",
                        canvas,
                        rendered.symbology,
                        payload,
                    )
                )
    return work


def _compare_read_rate() -> None:
    """Legacy (pyzbar+pylibdmtx) vs zxing-cpp: read rate + latency on hard codes."""
    work = _make_hard_workload()
    pyz, dmtx = PyzbarDecoder(), PylibdmtxDecoder()

    def legacy(img: np.ndarray, sym: Symbology) -> list[str]:
        dec = pyz if sym is Symbology.QR else dmtx
        hits = dec.decode(img) if sym is Symbology.QR else dec.decode(img, 100)
        return [h.payload for h in hits]

    engines: list[tuple[str, object]] = [("legacy", legacy)]
    try:
        from palletscan.pipeline.decoders import ZxingDecoder

        zx = ZxingDecoder()
        engines.append(("zxing", lambda img, _sym: [h.payload for h in zx.decode(img)]))
    except RuntimeError:
        print("(zxing-cpp not installed — pip install zxing-cpp to compare)\n")

    print(f"== Read rate on {len(work)} hard QR/DM codes (low pitch x motion blur) ==")
    print(f"{'engine':<10} {'reads':>8} {'rate':>7} {'p50 ms':>8} {'p95 ms':>8}")
    for label, fn in engines:
        reads, lat = 0, []
        for _name, img, sym, payload in work:
            t = time.perf_counter()
            got = fn(img, sym)
            lat.append((time.perf_counter() - t) * 1000)
            reads += payload in got
        print(
            f"{label:<10} {reads:>5}/{len(work):<2} {reads / len(work):>6.0%} "
            f"{statistics.median(lat):>8.1f} {_p95(lat):>8.1f}"
        )
    if len(work) < 20:
        print(f"(p95 linearly interpolated from n={len(work)} samples per engine)")
    print()


def _make_workload() -> list[tuple[str, np.ndarray, Symbology]]:
    """QR + DM crops at 3 pitches x 2 degradation levels, pasted on gray."""
    work = []
    for ppm in (3.0, 4.5, 6.0):
        for blur_modules in (0.0, 0.7):
            for sym_name, rendered in (
                ("qr", render_qr(f"PLT-{int(ppm*100):06d}", ppm)),
                ("dm", render_datamatrix(f"PLT-{int(ppm*100):06d}", ppm)),
            ):
                img = motion_blur(rendered.image, blur_modules * ppm)
                canvas = np.full(
                    (img.shape[0] + 80, img.shape[1] + 80), 120, np.uint8
                )
                canvas[40 : 40 + img.shape[0], 40 : 40 + img.shape[1]] = img
                work.append(
                    (
                        f"{sym_name}@{ppm}ppm/{blur_modules}mod",
                        canvas,
                        rendered.symbology,
                    )
                )
    return work


def _decode_one(item: tuple[str, np.ndarray, Symbology]) -> tuple[str, list[str]]:
    name, img, sym = item
    if sym is Symbology.QR:
        return name, [r.payload for r in PyzbarDecoder().decode(img)]
    return name, [r.payload for r in PylibdmtxDecoder().decode(img, 100)]


def _bench(executor_cls, workers: int, tasks) -> tuple[float, float, float]:
    latencies: list[float] = []
    start = time.perf_counter()
    with executor_cls(max_workers=workers) as ex:
        t0s = {ex.submit(_decode_one, t): time.perf_counter() for t in tasks}
        for fut, t0 in t0s.items():
            fut.result()
            latencies.append((time.perf_counter() - t0) * 1000)
    wall = time.perf_counter() - start
    return (
        wall,
        statistics.median(latencies),
        _p95(latencies),
    )


def main() -> int:
    _compare_read_rate()

    base = _make_workload()
    tasks = (base * (TASKS_PER_RUN // len(base) + 1))[:TASKS_PER_RUN]

    print("== Thread-safety stress check (concurrent vs serial results) ==")
    serial = sorted(_decode_one(t) for t in tasks)
    with ThreadPoolExecutor(max_workers=8) as ex:
        concurrent_res = sorted(ex.map(_decode_one, tasks))
    if serial != concurrent_res:
        print("MISMATCH: concurrent decode results differ from serial!")
        return 1
    print(f"OK: {len(tasks)} tasks identical under 8-way thread concurrency\n")

    print(f"== Throughput ({TASKS_PER_RUN} mixed QR/DM decode tasks) ==")
    print(f"{'executor':<10} {'workers':>7} {'wall s':>8} {'p50 ms':>8} {'p95 ms':>8}")
    results = {}
    for cls, label in ((ThreadPoolExecutor, "thread"), (ProcessPoolExecutor, "process")):
        for w in WORKER_COUNTS:
            wall, p50, p95 = _bench(cls, w, tasks)
            results[(label, w)] = wall
            print(f"{label:<10} {w:>7} {wall:>8.2f} {p50:>8.1f} {p95:>8.1f}")

    best_thread = min(results[k] for k in results if k[0] == "thread")
    best_process = min(results[k] for k in results if k[0] == "process")
    winner = "thread" if best_thread <= best_process else "process"
    print(
        f"\nRecommendation: decode.executor: {winner} "
        f"(thread best {best_thread:.2f}s vs process best {best_process:.2f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
