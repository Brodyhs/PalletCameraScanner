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
        statistics.quantiles(latencies, n=20)[18],  # p95
    )


def main() -> int:
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
