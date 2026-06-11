# PalletScan

Production-grade, 24/7 fixed-camera scanning that reads QR / Data Matrix
codes on pallets as forklifts carry them past stationary USB cameras.
**Phase 1** (this state of the repo): the complete core pipeline running on
synthetic input — no hardware required.

```
FrameSource → MotionGate → DecodeEngine → Dedup/PassTracker → EventBus → Sinks
                   ↘ RollingFrameBuffer (pre/post evidence) ↗
```

The operational invariant: **every pallet pass is accounted for** — it
either produces a decode event or a flagged miss with saved frame evidence.

## Quickstart (macOS dev)

```bash
brew install zbar libdmtx                  # native decoder libraries
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not acceptance"                 # fast suite (~5 s)
pytest                                     # full suite incl. 400-pass acceptance gate
palletscan synth --passes 20 --seed 7      # run the pipeline on synthetic passes
```

On Windows, skip the `brew` line — the `pyzbar`/`pylibdmtx` wheels bundle
the DLLs.

Outputs land under `./data/`: `events.jsonl`, `palletscan.db` (SQLite),
`evidence/` (JPEG bursts for missed passes), `truth.jsonl` (synthetic
ground truth).

## CLI

| command | what it does |
|---|---|
| `palletscan synth [--passes N] [--seed S] [--config Y] [--data-dir D]` | run the full pipeline on generated pallet passes |
| `palletscan version` | print version |

(`run`, `replay`, `calibrate`, `selftest` arrive with Phases 2–3.)

## Configuration

Single YAML file (see `config/default.yaml`, every key optional). The
synthetic envelope is calibrated by the two dimensionless ratios that govern
decodability:

- **px/module** (`px_per_module_range`, default 3–6): the optics envelope at
  the real 3–15 ft read distance.
- **blur in modules**: derived from speed × exposure / module size.
  `exposure_fraction: 0.03` (~1 ms at 30 fps) is the locked global-shutter
  operating point; raise it to stress-test.

Both ratios are logged per pass in `truth.jsonl`, so any read-rate failure
shows exactly where in the envelope it broke.

## Tests

- `pytest -m "not acceptance"` — unit tests per stage (fast dev loop).
- `pytest` — adds the end-to-end acceptance gate: 400 synthetic passes
  across the full spec envelope must reach **≥99.5% pass-level read rate**,
  and 100% of passes must produce a decode event XOR a miss event with
  on-disk evidence.

## Troubleshooting (macOS)

If `pyzbar`/`pylibdmtx` fail to load their dylibs:

```bash
export DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib
```

If a venv on macOS mysteriously stops finding installed packages, check for
hidden `.pth` files (`ls -lO .venv/lib/python*/site-packages/*.pth`) —
Python ≥3.12 silently skips `.pth` files carrying the `UF_HIDDEN` flag
(`chflags nohidden <file>` clears it). The code works around the known cases
(see `palletscan/_compat.py`).

## Layout

```
palletscan/
  cli.py            entry points
  config.py         pydantic models + YAML loading
  types.py          Frame/Roi/events/ground-truth dataclasses
  app.py            PipelineRunner: threads, queues, shutdown drain
  sources/          FrameSource ABC, SyntheticSource, pure render functions
  pipeline/         MotionGate, DecodeEngine, decoders, preprocess,
                    RollingFrameBuffer, PassTracker
  events/           EventBus, sinks (console/JSONL/SQLite), EvidenceWriter
  reliability/      bounded queues (watchdog/supervisor arrive Phase 3)
  web/              (dashboard arrives Phase 4)
tools/bench_decoders.py   ThreadPool-vs-ProcessPool measurement
tests/
```

See `ASSUMPTIONS.md` for every decision made without a spec answer.
