r"""Item 11 validation: the pygrabber mono survives repeated watchdog-style
reconnects from a NON-main (consumer) thread without a COM apartment crash.

This is the exact failure path the eval flagged P0: comtypes only CoInitializes
the import thread, but CameraSource.reopen()/close() run on the watchdog consumer
thread. The owner-thread rewrite must make each (re)connect self-initialize COM.

Runs the whole build -> stream -> reopen loop on a worker thread, then closes the
source from the MAIN thread (cross-thread release, the watchdog scenario).

Run with e-CAMView CLOSED:
  .\.venv\Scripts\python.exe tools\mono_reconnect_check.py [cycles]
"""
from __future__ import annotations
import sys, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import load_config  # noqa: E402
from palletscan.sources.camera import CameraSource  # noqa: E402


def _pull(src, n=8) -> int:
    it = src.frames()
    got = 0
    try:
        for _fr in it:
            got += 1
            if got >= n:
                break
    finally:
        it.close()
    return got


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    cfg = load_config("config/station.yaml")
    cam = next(c for c in cfg.cameras if c.id == "cam-mono")
    out: dict = {}

    def worker():
        try:
            src = CameraSource(cam)  # connect #1 (owner thread CoInitializes)
            out["src"] = src
            if not src._cap or not src._cap.isOpened():
                out["error"] = "initial connect did not open"
                return
            per_cycle = []
            for c in range(cycles):
                got = _pull(src, 8)
                per_cycle.append(got)
                if got < 5:
                    out["error"] = f"cycle {c}: only {got} frames before reopen"
                    return
                src.reopen()  # release old (teardown+CoUninit) + connect new (CoInit)
            out["per_cycle"] = per_cycle
            out["final"] = _pull(src, 8)  # after the last reopen
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=worker, name="fake-watchdog-consumer")
    t.start()
    t.join(timeout=180)
    if t.is_alive():
        print("FAIL: worker thread hung (possible COM deadlock in reopen/teardown)")
        return 1

    src = out.get("src")
    # cross-thread close: MAIN thread releases a capture built on the worker thread
    cross_close_ok = True
    if src is not None:
        try:
            src.close()
        except Exception as e:
            cross_close_ok = False
            print(f"cross-thread close() raised: {e!r}")

    if out.get("error"):
        print("FAIL:", out["error"])
        return 1
    print(f"reopen cycles ({cycles}) frames/cycle: {out.get('per_cycle')}")
    print(f"frames after final reopen: {out.get('final')}")
    print(f"cross-thread close ok: {cross_close_ok}")
    ok = (not out.get("error") and out.get("final", 0) >= 5 and cross_close_ok
          and all(g >= 5 for g in out.get("per_cycle", [])))
    print("\nRECONNECT (item 11):", "OK — N reopens from a worker thread, no COM crash"
          if ok else "PROBLEM")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
