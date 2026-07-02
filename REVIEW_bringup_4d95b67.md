# Adversarial review — bring-up commit `4d95b67` (bringup-2026-06 vs origin/main)

> **Scope:** `git diff origin/main...HEAD` — the single snapshot commit
> `4d95b67` "Windows bring-up: mono camera, multi-object tracking, decode + identity
> hardening" (~9,068 insertions / 231 deletions, 66 files), written 2026-06-20→23
> against real hardware, previously unreviewed.
> **Method:** 16 independent finder angles (5 diff shards, removed-behavior,
> cross-file, Python/COM pitfalls, wrapper/proxy, reuse, simplification, efficiency,
> altitude, 2× hardware-datasheet validation) → dedup → one adversarial verifier per
> candidate → gap sweep → verify. 91 agents total. Hardware angles cross-checked code
> against the e-con 37CUGM/24CUG datasheets, lens datasheets, extension-unit SDK
> manual, and extracted manual text — docs that did NOT exist in-repo when Phases 1–5
> were written.
> **Date:** 2026-07-01. **Reviewer:** Claude Fable 5 (multi-agent workflow).
>
> **Stats:** 110 raw candidates → 88 deduped → 70 verified (cap; 18 low-priority
> cleanup candidates unverified) → **68 survive (66 CONFIRMED, 2 plausible), 2 refuted**;
> sweep added 4 more CONFIRMED. **72 total findings.**
>
> **Independently reproduced during this review** (not just agent claims):
> - `tests/test_replay_acceptance.py::test_replay_of_recorded_clip_decodes_all_payloads`
>   **fails deterministically on this branch** (2 unaccounted passes) — Finding 1.
>   The commit's "559 green" covered only the fast suite, which excludes acceptance.
> - `list_devices()` from a non-main thread raises `CoInitialize has not been called`
>   and returns `[]` — Finding 3 (verified live in this venv).
> - Two static frames with σ=6 sensor noise: motion_frac 0.0 on main vs 0.138–0.73
>   on this branch — Finding 1's mechanism.

---

## Verdict in one paragraph

The bring-up's architecture is genuinely good — the pygrabber COM-owner-thread
capture design, the raw `IAMCameraControl` bindings, the flag discipline, the
identity-guard concept, and the injection harness idea are all keepers — but the
execution has **13 confirmed correctness bugs in `palletscan/` runtime code**, several
of which break the *default* (single-mode, legacy-engine) path that Phases 1–5
certified: the motion-gate downscale regression (breaks acceptance CI today), a
default-on payload gate that eats GS1 barcodes, and a COM-thread bug that silently
disables device identity on every reconnect. **Do not merge to main until the ranked
findings below are fixed.** The measurement/trial tooling (inject, soak, bench) has
its own accounting bugs and will mislead trial decisions until repaired. The
OPTICS_SPEC is high-leverage but internally contradictory in three places that
directly affect the purchase.

---

## Ranked findings (top 15 of 72)

1. **[CONFIRMED / correctness] `palletscan/pipeline/motion_gate.py:140`** —
   `_downscale` replaces INTER_AREA averaging with a strided pre-slice
   (`image[::sy, ::sx]`) that *decimates* instead of averages. At common resolutions
   (1920×1200, 1280×720, 960×540) the follow-up `cv2.resize` is a no-op, so raw
   per-pixel sensor noise floods the frame-diff — in **both single (default) and
   multi mode**, despite comments claiming semantics unchanged. Empirically: phantom
   motion_frac 0.138–0.74 on static noisy scenes; segments never close; consecutive
   passes merge. **Acceptance suite fails today** (`test_replay_acceptance`,
   `test_short_soak_invariants`, `test_demo_smoke_end_to_end`). On the real mono cam
   at gain 10 in dim light: permanent phantom segment, broken pass/miss accounting,
   decode CPU burned every frame. *Fix: restore the single INTER_AREA resize (or
   pre-slice only to an integer multiple then average).*

2. **[CONFIRMED / correctness] `palletscan/pipeline/decode_engine.py:186`** — the new
   **default-on** payload gate `_accept` rejects any payload containing C0 control
   bytes other than tab/LF/CR. **GS1 QR/Data Matrix payloads embed ASCII 29 (GS,
   FNC1/AI separator)** — standard warehouse label content — so decoded pallets
   finalize as MISSes. Docstring claims "no behavior change by default", which is
   false. Compounding (`:253`/`:267`): when a step's hits are all gate-rejected,
   `decode_frame` still short-circuits, skipping the remaining symbology and the
   variant fan-out for that frame. *Fix: allow GS (0x1D) — and probably RS/EOT for
   ISO 15434 — by default, and only short-circuit on non-empty accepted results.*

3. **[CONFIRMED / correctness] `palletscan/sources/devices.py:191`** — device
   enumeration never `CoInitialize`s its calling thread. `comtypes` auto-initializes
   only the importing (main) thread, so `list_devices()` **always** throws
   `CO_E_NOTINITIALIZED` (swallowed to `[]`) on the watchdog consumer thread that
   runs every `CameraSource.reopen()`. Consequences: name resolution falls back to
   bare `fallback_index` right after the replug that may have reordered indexes
   (wrong physical camera), the new identity guard gets `None` and is **inert on
   exactly the reconnect path it was built for**, and with station.yaml's own
   suggested `identity.policy: strict` every reopen raises → one USB glitch = 
   permanent outage. Verified live. *Fix: `CoInitializeEx` (+ paired uninit) inside
   `_list_windows`, or route enumeration through a COM-owning thread.*

4. **[CONFIRMED / correctness] `palletscan/calibrate.py:200`** — `run_calibration`
   has no pygrabber dispatch; `make_cap` always uses `cv2.VideoCapture`, which this
   very commit documents cannot open the Y8-only 37CUGM. **The mono camera can never
   be calibrated**, and calibrate is the step that stamps the identity fingerprint
   the new guard consumes.

5. **[CONFIRMED / correctness] `config/station.yaml:120`** — ships
   `decode.engine: zxing`, but zxing-cpp lives only in the optional `[zxing]` extra
   that no documented install path (RUNBOOK `pip install .`) pulls in. CI installs
   `.[dev]` only, so every zxing test `importorskip`s — **the deployed engine has
   zero automated coverage anywhere** (`.github/workflows/ci.yml:40`). The header
   comment "the ONLY change from defaults below is watchdog.max_outage_s" hides the
   second non-default.

6. **[CONFIRMED / correctness] `palletscan/sources/camera.py:441`** —
   `_guard_identity`'s vid:pid branch requires only `actual.vid` then formats
   `f"{vid}:{pid}"` → `'2560:None'`, which can never equal a validated fingerprint.
   The documented pid-absent composite-device case ("absence is unverifiable, never
   mismatch"; `parse_vid_pid('usb#vid_2560&mi_00') == ('2560', None)` is a pinned
   test) becomes a declared MISMATCH: warn-spam every connect, or strict-policy
   permanently refusing a correct camera.

7. **[CONFIRMED / hardware] `palletscan/calibrate.py:326` + `selftest.py:258`** —
   flipping `QUIRKS[MSMF].controls_reliable = False` (justified only for
   untrustworthy *readback*) also demoted the **exposure-EFFECT** hard gate. A
   physically dead exposure control now exits calibrate/selftest rc=0 with a warning
   that literally claims to be "trusting the exposure-effect check instead" even when
   that check just reported NO EFFECT. The enforcing tests were repointed MSMF→DSHOW
   instead of preserving the gate. Production mis-exposure now has no hard failure.

8. **[CONFIRMED / hardware] `palletscan/sources/pygrabber_capture.py:386`** — the
   pygrabber path **never programs frame rate into the device** (`choose_format`
   ignores `min/max_framerate`; `set_format` keeps the driver-default
   `AvgTimePerFrame`), yet `set(CAP_PROP_FPS)` stores the value, `get()` echoes it,
   and `apply_mode` logs fps as **verified=True from a mirror**. The honest number is
   the measured ~63 fps — 1.8 fps above selftest's hard 0.85×72=61.2 gate (standing
   flaky-selftest risk; see also station.yaml fps margin note in the refuted section).

9. **[CONFIRMED / correctness] `palletscan/pipeline/motion_gate.py:381–421`** —
   multi-mode merge handling is doubly broken: (a) `merge_streak` accrues for tracks
   matched 1:1 to their OWN blob (contention computed on the full candidate set), and
   with two mutually-contended blobs increments **twice per frame**, halving the
   hysteresis window; (b) the merge-commit reassigns `matched_tracks[keeper] = bi`
   without cleaning `matched_blobs` for the keeper's abandoned blob, so that real
   object's blob neither updates any track nor spawns a new one that frame. Result:
   side-by-side pallets force-close a correctly-tracked segment mid-zone (premature
   MissEvent), the blob re-spawns and re-debounces, one physical pass → two segments.
   Related: `:480` `_spawn_track` sets `active_streak=1` without evaluating the open
   condition, breaking `open_frames=1` parity with single mode.

10. **[CONFIRMED / correctness] `palletscan/station.py:274` + `:290`** — under the
    new `continue_others` policy, a business-bus drain failure is swallowed whenever
    any arm error exists (`if not business_drained and not self._errors` guard), so
    the `bus_errors` check at `:289` can never see it — contradicting the code's own
    "a business-bus drain failure is never tolerated". And CONTINUE_OTHERS tolerates
    `WatchdogEscalation`, converting the watchdog's never-give-up loop into permanent
    arm loss: no respawn, no exit-3 supervisor restart.

11. **[CONFIRMED / correctness+hardware] `palletscan/sources/pygrabber_capture.py:177–230`**
    — constructor-timeout lifecycle races: the abandoned owner thread is never joined
    and `_stop` is checked nowhere in the graph-build path, so it finishes building,
    calls `graph.run()` (streaming the **exclusive** UVC device), and `:211`
    unconditionally flips `_opened=True` on the already-released object. Every
    watchdog retry leaks another permanently-blocked COM-initialized thread; a zombie
    graph can hold the device against all subsequent opens. Also `:230`: `_run`'s
    `finally` calls `CoUninitialize` while frame locals still hold live DirectShow
    interface refs (released by `__del__` after return) — undefined behavior per COM
    apartment rules.

12. **[CONFIRMED / correctness] injection-harness accounting (`palletscan/sources/inject.py`,
    `tools/soak.py:598`)** — the trial-rehearsal numbers are wrong in four ways:
    `inject.py:239` records truth in live-camera frame_index space while yielding
    wall-clock ts, breaking the `reconcile_truth` contract (every genuinely missed
    injected pass → "unaccounted" in `report.problems`); `:221` watchdog
    discontinuity discards in-flight passes from truth though their frames were
    already delivered; `:224` `launch_in` decrements while `max_concurrent` blocks,
    so a long pass makes the next launch with ZERO idle gap; `:66` `_trajectory` uses
    max instead of min of per-axis exit times (diagonal passes stay "active" while
    compositing nothing); `:85` `model_copy(update=...)` skips validation → `None`
    width/height/fps crashes with an opaque TypeError.

13. **[CONFIRMED / correctness] `palletscan/sources/pygrabber_capture.py:110`** —
    `choose_format` ranks "any Y8" above "exact configured resolution": a resolution
    the camera offers only in Y12 opens Y8 at different geometry → first frame trips
    `_verify_frame_shape` → deterministic infinite watchdog reconnect loop.

14. **[CONFIRMED / hardware] `config/station.yaml:92` + `OPTICS_SPEC.md:62,90`** —
    the numbers that drive the purchase are inconsistent: cam-mono `exposure: -8`
    (3.9 ms per the 37CUGM manual's slider table) yields 0.7 modules of blur at 2 mph
    and 1.7–3.5 at 5–10 mph against the spec's own <0.5-module budget (its table:
    5 mph needs ≤1.12 ms); Section 8's conclusion "1–4 ms is the correct design band"
    contradicts that same table, and the 2,000–4,000 lux buy target is 2–4× under the
    ~7,800 lux its own table requires at the 1 ms that actually freezes the band; the
    Section 5 DoF table is computed at ~7 ft focus, not the ~5 ft Section 9 instructs
    — at 5 ft focus the 8 mm mono DoF is ~3.9–7.1 ft, never reaching the 8 ft far
    edge. *Fix the spec before buying lights; buy to the 1 ms row or explicitly
    de-scope 5–10 mph.*

15. **[CONFIRMED / efficiency] `palletscan/app.py:406–410` + `motion_gate.py:294`** —
    multi-mode runs a full `decode_frame` per open track, each with a **fresh**
    `frame_budget_ms` (budget × up to `track_max_objects=8` on the single pipeline
    thread), and `result_tracks` includes tracks UNMATCHED this frame, so stale ROIs
    keep getting decoded during quiet frames. With the zxing engine there is **no
    per-call timeout at all**, making the overrun unbounded. Frames drop during
    exactly the multi-object traffic the feature exists for.

---

## Remaining confirmed findings (16–72, grouped)

### Capture / sources (`palletscan/sources/`)
- `pygrabber_capture.py:293` — `_apply_control` for CAP_PROP_AUTO_EXPOSURE returns
  the sentinel as successful readback even when `_cam_ctrl is None` or Get/Set raised
  (swallowed at `:291`) — set() reports success for a flag that never reached hardware.
- `pygrabber_capture.py:211` — [PLAUSIBLE] owner thread's `_opened = bool(got)` can
  overwrite release()'s `_opened = False` without checking `_stop` (check-then-act race).
- `pygrabber_capture.py:201` — SampleGrabber left at pygrabber's hardcoded RGB24
  media type: every mono Y8 frame is color-converted by an inserted DirectShow filter,
  full-copied by BufferCB (9.6 MB @ 2064×1552), then copied again by
  `ascontiguousarray(image[:,:,0])` (3.2 MB) — per frame, ~63 fps. Configure the
  grabber for Y800/GREY or slice without the RGB round-trip.
- `camera.py:474` — strict-policy identity raise happens after `self._cap = cap` was
  published; construction path never releases → leaked open device; pygrabber owner
  thread holds a ref so the graph streams forever.
- `camera.py:306` + `controls.py:193` — a control write outright REJECTED
  (`accepted=False`) on a controls-unreliable backend files into the INFO
  "asserted but unverifiable" bucket (the failed-bucket filters on `r.verifiable`),
  never warns, never bumps `connect_mismatches`; `_set` even returns a note claiming
  "applied request=..." for a rejected write — even on pygrabber, where set() returning
  False is device-confirmed failure.
- `devices.py:138` — `devices_from_monikers` wraps only the DevicePath read in
  try/except; one moniker whose FriendlyName read raises COMError aborts the whole
  enumeration → `[]` via the blanket except.

### Pipeline / decode
- `decode_engine.py:204` — the payload gate logs a WARNING per rejected payload per
  frame; recurring decoder false positives (the "F'm" phantom the gate was built for)
  turn it into per-frame logging I/O on the pipeline thread.
- `app.py:429` — the opt-in idle scan constructs a fresh `PassDecodeContext()` every
  invocation, so `frames_attempted` is always 0 and the step-3 variant fan-out can
  never engage for static codes; it also runs synchronously on the pipeline thread
  where the legacy pyzbar step has no timeout (full 3.2 MP scan ≫ frame_budget_ms).
- `config.py:737` — `frame_queue_size` has no validator; ≤0 silently turns
  DroppingQueue into an unbounded queue that never drops.

### Tools (measurement integrity)
- `soak.py:66` — `SNAPSHOTS_PATH` hardcoded, ignores `--data-dir`, opened append →
  successive runs interleave.
- `bench.py:659` — tile labeled "decodes/s" displays attempts/s.
- `bench.py:411` — `/control` appends to `bench._cmds` from the FastAPI thread
  WITHOUT the lock the capture thread uses to snapshot+clear → commands lost.
- `bench.py:330` — capture read-failure path is a bare `continue`: disconnect
  busy-spins while the UI shows a green LIVE dot and stale fps.
- `bench_decoders.py:81` — hard workload is 18 items; the `len(lat) >= 20` guard
  means the "p95 ms" column always prints max().
- `bench_sim.py:53` — "same number of decode chances per speed" is false: passes
  enter at x=0 on a 960 px canvas and exit mid-pass at higher speeds (~half the
  chances of the 1920 px camera it's compared against).
- `expo_probe.py:71` — `motion_box` called with hardcoded (1920, 1200) instead of
  `gray.shape`; wrong ROI scaling at any other negotiated resolution.
- `inject_grid.py:285` — `_SharedLiveInner` wraps `build_camera_source()`'s return
  (the WatchdogSource, whose `frames()` is single-use and raises on reuse) —
  contradicting its own docstring; shared-camera grid fails on the second point.

### Tests / CI
- `tests/pygrabber_fakes.py:332` — the fake `comtypes` module is dead code:
  `pygrabber_capture.py` does `import comtypes` at module top before the fixture
  runs, so the owner thread always calls REAL CoInitialize/CoUninitialize — the
  lifecycle suite's "NO real COM" guarantee is false.
- `tests/pygrabber_fakes.py:366` — double `install()` in one test permanently
  corrupts sys.modules for the session (finalizer restores the fakes, not the reals).
- `tests/pygrabber_fakes.py:271` — FakeFilterGraph delivers every frame
  unconditionally while real BufferCB delivers only when `keep_photo` is armed —
  the one-shot re-arm contract that keeps frames flowing is never exercised by any test.
- `tests/test_pygrabber_capture_marshal.py:128` — `_command_loop` never executes
  anywhere in the suite (marshal test hand-rolls its own worker; lifecycle fakes have
  no QueryInterface so `_has_controls` is always False).
- `tests/test_devices.py:131` — `test_windows_enumeration_failure_returns_empty_loudly`
  only passes where `import pygrabber` fails (macOS); on the Windows CI job and the
  factory box it re-imports from disk, performs REAL COM enumeration from a unit
  test, and fails (this is one of the known "4 pre-existing failures", but the commit
  added windows-latest CI that inherits it).
- `.github/workflows/ci.yml:40` — CI installs `.[dev]` only → zxing tests all skip →
  the production engine has zero CI coverage (also CI pins Python 3.11 vs factory 3.13.1).

### Efficiency / cleanup
- `inject.py:216` — `frames()` calls uncached `_plan(0)` just to read
  `idle_frames_before`: pass 0's full patch (QR/DM render, warp, two blur passes) is
  rendered, discarded, re-rendered at launch; every launch renders inline between
  live camera reads.
- 18 further cleanup/reuse/simplification candidates were deduped but not verified
  (verification cap); dominant themes: the 14 new tools re-implement pipeline wiring
  that `app.py`/`station.py` expose, `bench.py` (766 lines) duplicates motion
  segmentation + miss tracking + auto-gain outside the product, and `live_decode.py`
  (called "throwaway NOT product code" by its own handoff) is committed.

## Refuted (2)
- ~~`pygrabber_capture.py:301` brightness routed to a control the 37CUGM lacks~~ —
  verifier found the claim unsupported against the docs/config as shipped.
- ~~`station.yaml:87` dual-camera USB bandwidth over one controller~~ — kept only as
  PLAUSIBLE context (see finding 55 in the workflow output); the fps-margin half was
  refuted as stated but the ~63 vs 72 fps headroom concern is real via Finding 8.

---

## What's good (keep all of this)

- **`pygrabber_capture.py`'s single-owner-COM-thread architecture** — the right
  design for apartment-bound DirectShow under a multi-threaded watchdog; hides behind
  the existing `Capture` protocol so CameraSource/watchdog/settings run unchanged;
  ~770 lines of fake-based tests let CI cover it without hardware. The *lifecycle
  edges* are buggy (findings 3, 11, 13), not the architecture.
- **`dshow_controls.py`** — raw comtypes `IAMCameraControl`/`IAMVideoProcAmp`
  declarations instead of a vendor SDK; preserves the pip-only posture while making
  mono exposure/gain settable.
- **Flag discipline held**: `motion.tracking` defaults `single`, `decode.engine`
  defaults `legacy` (import-guarded, optional extra), `idle_scan_s=0.0`,
  `identity.policy=warn`, `on_arm_failure=stop_all`; deployment choices isolated in
  `station.yaml`. (The two default-path leaks — motion-gate downscale and the payload
  gate — are bugs against this discipline, not evidence it was absent.)
- **The identity-guard concept** (CameraIdentity + calibrate fingerprint stamping)
  directly closes the MSMF pinned-index wrong-camera hole the handoff flagged. It
  needs findings 3, 4, 6 fixed to actually function.
- **Multi-object tracking closes a real invariant hole** — a decoded pallet can no
  longer swallow a co-located undecoded pallet's MissEvent; 392-line dedicated test
  file. The association/merge internals need the finding-9 cluster fixed.
- **The injection harness family** — hardware-in-the-loop rehearsal with truth
  reconciliation and explicit honesty caveats in every docstring. Needs the
  finding-12 accounting fixes before its numbers mean anything.
- **OPTICS_SPEC.md** — the highest-leverage trial-readiness artifact in the commit:
  proves the stock 3 mm lenses fail the 5 px/module floor beyond ~3–4 ft and derives
  the 8 mm M12 buy. Needs its three internal contradictions fixed (finding 14).
- **First-ever CI** (windows-latest, camera-free) and first-ever version control of
  the folder; honest snapshot commit message.
- **`metrics.py` `sorted(list(deque))` fix** — real /stats.json-500-under-load bug
  evidently caught on hardware.

## Strategic concerns (beyond line-level bugs)

1. **Process bypass.** This repo's whole quality story is plan → execute →
   adversarial review → fix. `4d95b67` is a ~9k-line unreviewed commit mixing
   hardware-forced necessity (pygrabber) with elective new subsystems (multi-object
   tracking, idle scan, station policy, injection harness) no plan sanctioned. This
   review closes that gap — the findings above are what the house loop exists to catch.
2. **zxing promoted on weak evidence.** `station.yaml` ships `decode.engine: zxing`
   off an 18-crop synthetic bench ("100% vs 78%"), far below PLAN_PHASE6's own
   promotion bar; the zxing path abandons the budget machinery entirely (no per-call
   timeout), so the budget-bounded cascade Phases 1–5 were reviewed around no longer
   bounds the deployed path.
3. **CI never tests the deployed configuration** — legacy engine + single tracking +
   Python 3.11, vs the factory's zxing + pygrabber + 3.13.
4. **Documentation ledger broke.** ASSUMPTIONS.md (#1–60) and RUNBOOK.md untouched
   despite a new backend, engine, guard, policy, and knobs; ARRIVAL_CHECKLIST boxes
   unticked; hardware truths live only in handoff prose and yaml comments.
5. **Dual-camera drift.** A/B has never run on hardware; the time-based
   motion-debounce fix the handoff called required "before trusting A/B" was not
   made; arms run ~55 vs ~63 fps with frame-count debounces.
6. **Trial/replay gap.** PLAN_PHASE6 6.1 (record real segments + offline replay) was
   not built and the injection harness is explicitly not a substitute — a trial today
   produces no re-analyzable corpus of real misses.

## Phase 6 — revise, don't execute as written

PLAN_PHASE6.md's foundation ("465 tests UNMODIFIED", single-open-segment PassTracker
line numbers) is stale — the bring-up moved to a dict of concurrent segments and a
559-test baseline. Recommended reorder:

1. **Revised 6.1 — trial recording + replay** (now the single most valuable item):
   re-derive SegmentRecorder against the multi-segment PassTracker (per-candidate
   pending records, per-track ROI in meta); rebaseline at 559. Recording real
   segments is the only ground truth the injection harness cannot provide, and
   OPTICS_SPEC predicts optics-driven failures the injection model won't show.
2. **6.4 — last-chance decode + RESPONSE_PLAYBOOK.md** (survives intact, gets
   cheaper with a sub-ms zxing): fold in the OPTICS_SPEC physical interventions
   (8 mm lens swap, lighting targets, exposure operating points) and the new flags
   (`tracking: multi`, `engine: legacy` revert, `idle_scan_s`).
3. **6.2 reshaped into promotion evidence**: a real legacy-vs-zxing bench on
   generated standard + hard corpora (the 18-crop compare is not it) plus a measured
   worst-case zxing latency bound on full-res 2064×1552 mono ROIs — either a
   budget/downscale guard in the zxing branch or a recorded ASSUMPTIONS entry for
   why none is needed. **WeChat/opencv-contrib demoted to contingency backlog** —
   spending that dependency-swap risk before owning a real miss corpus is backwards.
4. **6.3 folded into `inject_grid`** (which already does single-axis envelopes with
   the real camera in the loop); keep only as a cheap offline variant if wanted.

## Recommended sequence from here

1. **Fix the merge blockers on `bringup-2026-06`** (findings 1–13; ~a day of
   focused work — most are small): motion-gate downscale, payload gate GS1 +
   short-circuit, CoInitialize in enumeration, calibrate pygrabber dispatch,
   station.yaml/[zxing] install + CI leg (`pip install -e ".[dev,zxing]"`, Python
   3.13), identity pid-None, exposure-effect gate restoration, fps honesty,
   merge-cluster, station policy, ctor-timeout lifecycle, injection accounting,
   choose_format ranking. Re-run the FULL suite including acceptance.
2. **Merge to main as the snapshot + fix commits** (don't rewrite history), with
   backfilled ASSUMPTIONS entries (pygrabber decision, zxing status, identity guard,
   station policy, mono ~63 fps reality) and RUNBOOK updates (mono section, install
   incl. extras, engine-revert procedure).
3. **Fix OPTICS_SPEC's three contradictions, then buy** — 8 mm M12 lenses ×2 and
   lighting sized to the 1 ms row (~7,800 lux) or an explicit de-scope of 5–10 mph;
   lead time is on the critical path and every read-rate number collected with the
   3 mm lenses beyond ~4 ft measures the wrong bottleneck.
4. **Hardware validation pass, in order**: time-based motion debounce → dual-camera
   A/B; ARRIVAL_CHECKLIST §6 unplug/replug on pygrabber mono under the watchdog
   (post-fix 3); §7 30-min stability + measure_cpu with zxing + both cameras; §9
   Windows ops (service, CTRL_BREAK, job-object, stop-latch, exit-4).
5. **Rewrite PLAN_PHASE6.md** per the section above and execute revised-6.1 first.

---

*Full per-finding verifier reasoning and the 4.6M-token workflow journal:*
`C:\Users\brody\AppData\Local\Temp\claude\...\tasks\w35fstbo9.output` and the
workflow transcript dir (`wf_cc92fa42-02d/journal.jsonl`).
