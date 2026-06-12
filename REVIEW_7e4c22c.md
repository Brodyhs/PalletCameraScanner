# Code review — Phase 4 commit `7e4c22c`

**Scope:** `git diff HEAD~1 HEAD` at `7e4c22c` ("Phase 4: dashboard, MJPEG live view, A/B dedup + report, manifest reconciliation — 312 tests green"). ~4,546 insertions across 37 files.

**Method:** xhigh-effort recall review. 9 independent finder angles (5 correctness, 3 cleanup, 1 altitude) → 26 deduplicated candidates → one adversarial verifier per candidate (2 refuted with live repros: a claimed SQLite WAL-pragma lock race and a claimed report-window UTC bug) → fresh gap sweep → 5 more candidates, all verified. Final: 17 confirmed/plausible correctness findings, 7 confirmed cleanups. Findings 1 and 2 were **reproduced live** by verifiers, not just argued.

**Status:** Documentation only — no fixes applied in this review.

---

## Findings (ranked most-severe first)

### 1. Cross-camera dedup double-counts when one camera lags

- **file:** `palletscan/events/dedup.py`
- **line:** 151
- **summary:** CrossCameraDeduper._prune evicts payload state by a single global high-water cutoff fed by all cameras, so a lagging camera's pass that is still inside the merge window finds its state pruned and is emitted as a brand-new business pass — the double count the deduper exists to prevent.
- **failure_scenario:** Reproduced: window_s=12; camA pallet P @ts=100, camA pallet Q @ts=113 advances high_water (cutoff=101, P evicted), camB pallet P @ts=100.5 arrives late (decode backlog/watchdog reconnect) -> passes_emitted=3, cross_camera_merges=0, two SQLite rows for one pallet; A/B report business passes inflated. Control without Q correctly merges (1 pass, 1 merge). Eviction must key on the same per-event timestamp the merge predicate at line 96 uses, not a cross-camera global.

### 2. Unguarded prune cleanup silently eats a MissEvent

- **file:** `palletscan/events/evidence.py`
- **line:** 152
- **summary:** prune()'s trailing empty-day-directory cleanup loop has none of the OSError tolerance this commit added to _candidate_dirs/_dir_size, so a concurrent-deletion race aborts the miss write and the MissEvent is silently lost — the exact failure ASSUMPTIONS #43 claims was eliminated.
- **failure_scenario:** Reproduced: concurrent dir churn raised OSError(ENOTEMPTY) from day.rmdir() out of prune() in 2 iterations. The exception propagates prune -> write_burst -> _finalize_miss after the pending miss was already popped (pass_tracker.py:174), before the emit at pass_tracker.py:277; app.py:362 swallows it as frame_errors += 1. A seen-but-undecoded pallet vanishes from the events DB; miss counts deflated, read rate inflated.

### 3. Miss gallery falsely reports all evidence as pruned under cwd mismatch

- **file:** `palletscan/web/app.py`
- **line:** 89
- **summary:** _miss_images resolves the stored (relative) evidence_dir and the evidence root against the dashboard process's cwd, so reviewing a trial from a different cwd makes relative_to() raise for every miss and the gallery renders 'evidence pruned' even though all evidence exists on disk.
- **failure_scenario:** Reproduced: trial run with default evidence.dir stores 'data/evidence/2026-06-12/camA-000123' (pass_tracker.py:285); later `palletscan dashboard --data-dir /opt/station/data` from $HOME resolves it against $HOME -> ValueError swallowed into images=[] -> app.js:182 renders 'evidence pruned' for every miss. Reviewers conclude evidence is gone while the /evidence static mount could serve it.

### 4. ReadStore startup failures escape the clean exit-2 path as raw tracebacks

- **file:** `palletscan/web/store.py`
- **line:** 45
- **summary:** ReadStore eagerly runs sqlite3.connect + CREATE TABLE DDL with no parent-dir mkdir (unlike SqliteSink._connection) and no error mapping, so dashboard startup failures escape the CLI's clean exit-2 handling as raw sqlite3.OperationalError tracebacks.
- **failure_scenario:** Reproduced both paths: (1) `palletscan dashboard --data-dir` on a chmod-444 events DB passes the is_file() check then crashes with 'attempt to write a readonly database' from executescript (store.py:42) — _cmd_dashboard only catches DashboardServerError around server.start(); (2) run/synth/replay --dashboard with a sinks.sqlite.path whose parent dir doesn't exist crashes with 'unable to open database file' at cli.py:107, outside the try that catches _DashboardUnavailable, before the run starts — the same config without --dashboard works.

### 5. Outbox backlog metric is invisible in A/B mode

- **file:** `palletscan/station.py`
- **line:** 126
- **summary:** In A/B mode the store-and-forward outbox metric disappears entirely: HttpSink is attached only to the business bus (no MetricsRegistry) and per-camera runners get only ForwardingSink, so the sole set_outbox_probe call site (app.py:284) never executes.
- **failure_scenario:** sinks.http.enabled with two cameras; the HTTP endpoint goes down and the outbox backs up for hours -> every cameras.<id>.outbox in /stats.json is null, the dashboard outbox tile (gated on snap.outbox, app.js:96) never renders, and --stats-interval logs show nothing — the backlog signal is invisible in exactly the trial mode the dashboard was built for, while single-camera runs show it.

### 6. Poll loop destroys in-progress review notes

- **file:** `palletscan/web/static/app.js`
- **line:** 207
- **summary:** The ~6 s poll calls refreshMisses() which rebuilds every miss card via grid.replaceChildren(), resetting each review-note input to server state and silently destroying the operator's in-progress text.
- **failure_scenario:** Operator types a note into a miss card; within 6 s loop() re-runs refreshMisses(), the card is rebuilt with note.value = miss.review_note || '' (line 189), and the typed text (read only at button-click time, line 192) is wiped mid-keystroke. No activeElement guard, diffing, or draft cache exists.

### 7. Failed review POST is silently dropped

- **file:** `palletscan/web/static/app.js`
- **line:** 153
- **summary:** markReviewed uses raw fetch with no resp.ok check (unlike the getJSON helper) and is invoked with its promise discarded, so a failed review POST is silently dropped and the operator believes the review was saved.
- **failure_scenario:** POST /api/misses/{id}/review returns 500 (store.mark_reviewed's INSERT/commit has no sqlite error handling, store.py:100-111, unlike the read paths) -> no error shown, refreshMisses re-renders the miss unreviewed; a network-level failure becomes an unhandled promise rejection visible only in the console.

### 8. Revision-hammer test can wedge the pytest process on failure

- **file:** `tests/test_dedup.py`
- **line:** 258
- **summary:** The revision-hammer test's finally block joins its two non-daemon submitter threads with timeout=10 but never calls barrier.abort(), so any mid-round assertion failure leaves both threads parked forever on the 3-party barrier and pytest hangs at interpreter exit.
- **failure_scenario:** Reproduced with a structural repro: on a loaded CI box the 'bus did not drain in time' assert (line 242) fires between barrier phases on rounds 0-28 -> join(timeout=10) returns with threads alive -> threading._shutdown blocks joining non-daemon threads -> the job wedges until CI timeout (no pytest-timeout configured), burying the printed failure.

### 9. Reconciliation panel claims "no manifest loaded" on any transient error

- **file:** `palletscan/web/static/app.js`
- **line:** 310
- **summary:** refreshReport's catch renders 'no manifest loaded' for ANY failure of /api/report/reconciliation (500, network drop, server restart), not just the 404 no-manifest signal, and the /api/report/ab catch silently leaves a stale table.
- **failure_scenario:** Manifest is uploaded and the panel populated; a transient network drop during the 6 s poll throws from getJSON (which throws identically for 404/500/rejection, app.js:39) -> the panel is replaced with 'no manifest loaded' although one is stored in SQLite, misleading the operator into re-uploading; a simultaneous ab-report failure leaves a silently stale comparison table.

### 10. One MJPEG error permanently kills the live tile

- **file:** `palletscan/web/static/app.js`
- **line:** 59
- **summary:** A single MJPEG <img> error permanently replaces the live tile with 'live view unavailable' — img.onerror removes the element, there is no retry, and the liveGridBuilt latch (lines 106-108) means the grid is never rebuilt for the session.
- **failure_scenario:** Any exception escaping render_jpeg inside the /live frames() generator (web/app.py:135-144) or a TCP reset aborts the multipart stream mid-flight -> browser fires img.onerror -> tile dies for good while the server keeps streaming and stats keep updating; only a manual page reload recovers.

### 11. Manifest upload silently corrupts non-UTF-8 payloads

- **file:** `palletscan/web/app.py`
- **line:** 188
- **summary:** The manifest upload decodes the body with errors='replace', silently mangling non-UTF-8 (e.g. Excel cp1252) payload bytes into U+FFFD instead of rejecting the upload, producing expected payloads that can never match a scan.
- **failure_scenario:** Reproduced: cp1252 body 'PLT-MÜNCHEN-01' stores as 'PLT-M�NCHEN-01'; reconcile() matches by exact string equality -> missing=['PLT-M�NCHEN-01'], unexpected=['PLT-MÜNCHEN-01'], true_read_rate=0.0, with a success response at upload time. The file-path fallback (store.py:138) uses strict UTF-8 and rejects the same bytes — the two ingestion paths are inconsistent. (Payloads are ASCII-only today per ASSUMPTIONS #8, so latent until the real ID scheme lands.)

### 12. synth --ab loses the CLI's RuntimeError message/exit-code contract

- **file:** `palletscan/cli.py`
- **line:** 397
- **summary:** The new synth --ab block calls station.run() without the except-RuntimeError mapping _cmd_run has, defeating station.py's own promise (comment at station.py:198-200) that runner failures survive to the CLI's clean message/exit-code contract.
- **failure_scenario:** Disk fills during synth --ab; evidence meta.json write raises OSError in the tracker flush -> PipelineRunner.run wraps it in RuntimeError (app.py:421) -> StationRunner.run re-raises (station.py:201-203) -> no handler in _cmd_synth -> raw traceback instead of the 'run: <msg>' + mapped exit code the equivalent failure produces under palletscan run.

### 13. Evidence URLs are not percent-encoded

- **file:** `palletscan/web/app.py`
- **line:** 96
- **summary:** _miss_images interpolates camera-id-derived filesystem segments into /evidence URLs with no percent-encoding (and app.js assigns them raw to img.src/href, unlike /live which uses encodeURIComponent), so a legal camera id containing '#', '?', or '%' breaks every miss thumbnail.
- **failure_scenario:** cameras[].id = 'cam#1' (validation only requires non-empty, config.py:393-395; the id flows verbatim into candidate_id and the evidence path) -> img.src '/evidence/cam#1/...' is truncated at the fragment marker -> all miss-gallery thumbnails and links 404 for that camera while its live view works.

### 14. Manifest upload gives no feedback on network-level failure

- **file:** `palletscan/web/static/app.js`
- **line:** 337
- **summary:** uploadManifest is wired as a bare onclick with its promise discarded and no try/catch, so a network-level fetch rejection (or file.text()/resp.json() rejection) is unhandled and #manifest-status keeps its stale text (resp.ok IS checked, so HTTP errors do surface).
- **failure_scenario:** Operator clicks upload as the server restarts -> fetch rejects -> neither the ok nor the error branch runs, the status line keeps its previous text, the rejection is console-only, and the operator proceeds to reconciliation believing the manifest was stored.

### 15. Partial StationRunner construction leaks opened camera devices

- **file:** `palletscan/station.py`
- **line:** 117
- **summary:** StationRunner.__init__ builds camera sources and runners with no cleanup on partial failure, and CameraSource.__init__ opens the device eagerly (camera.py:115 'fail fast'), so a failure on source 2, the duplicate-id ValueError, or a runner constructor error leaves source 1's capture open with no deterministic release.
- **failure_scenario:** cameras [camA, camB] with camB unplugged -> camA's cv2.VideoCapture is opened, then create_source(camB) raises and __init__ propagates without closing camA. Low impact today (the only call site exits the process; CPython refcounting eventually releases it), but any embedder or retry loop that retains the exception keeps the device locked — there is no __del__/context-manager/close path anywhere in the package.

---

## Cut by the 15-finding cap (confirmed mechanisms, lower severity)

### 16. Silent in-window eviction at the _MAX_TRACKED cap

- **file:** `palletscan/events/dedup.py`
- **line:** 158
- **summary:** When the tracked-state map exceeds _MAX_TRACKED (4096, not configurable), the cap-eviction loop deletes the oldest payload state — which after the cutoff prune is necessarily still inside the merge window — with no counter increment and no log, so the evicted payload's next sighting from the other camera emits a second business event.
- **failure_scenario:** dedup.window_s is operator-configurable with no upper bound (config.py:199, no validator); with a grossly large window (>4096 distinct payloads in flight — roughly a 10-hour window at the documented ~7 events/min) each new submit silently evicts in-window state and the next sighting double-counts with zero observability. Extreme trigger, but the missing counter/log violates the project's own "counted, logged drops" convention (README:127-128) and the "every pallet pass is accounted for" invariant.

### 17. DashboardServer start-after-stop silently serves nothing (latent)

- **file:** `palletscan/web/server.py`
- **line:** 97
- **summary:** stop() sets self._server.should_exit = True and nulls self._thread but never resets should_exit or rebuilds the single-use uvicorn.Server, so a second start() after a clean stop() passes the 'already started' guard, reports success (uvicorn's started flag is never reset), and the serve thread exits within a tick.
- **failure_scenario:** Reproduced: server.start(); server.stop(); server.start() returns without error, the thread is dead within 1 s, and every request gets connection refused. Latent today — all five CLI call sites build a fresh instance and stop exactly once in a finally, and no test restarts a server — but the public start/stop API invites the pattern. (Verdict: PLAUSIBLE.)

---

## Deferred cleanups (all verified; quality only, no fixes this session)

1. **`palletscan/cli.py:304/388/408/528` + `465` — dashboard lifecycle quadruplication.** The block `dashboard = None; if args.dashboard or cfg.web.enabled: try _start_dashboard / except _DashboardUnavailable: print + return 2; try: runner.run() finally: dashboard.stop()` is copy-pasted four times (_cmd_run, both _cmd_synth branches, _cmd_replay), differing only in the message prefix; _cmd_dashboard (465-478) additionally hand-builds the DashboardContext/ReadStore/DashboardServer + error mapping that _start_dashboard already encapsulates. One contextmanager helper taking the command name covers all five sites.

2. **`palletscan/web/preview.py:66` — full JPEG encode per client per tick.** render_jpeg runs cvtColor + overlay draw + resize + imencode unconditionally, and the /live generator (web/app.py:136-139) only compares the returned stamp after the encode — so unchanged frames are fully encoded then discarded, per client, at preview_fps (default 10 Hz). Fix: cache encoded bytes keyed by stamp inside LivePreview (the stamp/refs are already snapshotted atomically under _lock, so the cache is race-safe) and let the generator pre-check the cheap stamp property; the 1 s keepalive serves cached bytes.

3. **`palletscan/web/store.py:155` — unfiltered full-table scan + per-row json.loads on every poll.** pass_and_miss_rows does `SELECT kind, detail_json FROM events` with no WHERE and json.loads every row; the dashboard polls two report endpoints backed by it every ~6 s, so cost grows linearly for the trial's life. _reconciliation (web/app.py:215) needs only pass payloads — `SELECT payload FROM events WHERE kind='pass'` is exactly equivalent and served by idx_events_kind. The A/B report genuinely needs parsed detail_json, so cache parsed rows keyed by MAX(rowid) or fetch incrementally by rowid there.

4. **`palletscan/station.py:125` — 100k-cap _ListSink retained on live runs that can never use it.** The collector is attached to the business bus unconditionally, but its events list is read only by truth reconciliation, gated on every source being synthetic (station.py:210). Live A/B trials retain up to 100,000 business events (~tens to ~200 MB at cap) for nothing. Attach it only when all sources are synthetic (decidable at construction; StationSummary's collector_dropped needs a 0 default). Also: station.py:32 imports the underscore-private _ListSink from palletscan.app — fragile cross-module coupling.

5. **`palletscan/events/sinks.py:197` + `palletscan/station.py:95` + `palletscan/types.py:142` — revision special-cased per event type.** The SQLite schema and upsert guard are kind-agnostic, but the serializer hardcodes `0  # misses are never re-emitted` for MissEvent and _business_view hand-rolls max-revision-wins with isinstance(PassEvent) dispatch. Add `revision: int = 0` to MissEvent (or a shared Event contract): both sites become uniform, semantics identical, and the compat audit found nothing relying on the field's absence (additive per ASSUMPTIONS #41; DB column already defaults miss rows to 0).

6. **`palletscan/station.py:77-84` — reconciliation rendering duplicated from `palletscan/app.py:118-125`.** StationSummary.format reproduces RunSummary.format's truth-passes/decoded/missed/UNACCOUNTED block character-for-character apart from a two-space indent (the unaccounted property is also copied verbatim, station.py:58-60 vs app.py:97-99). Extract a `_format_reconciliation(r, indent="")` helper in app.py; station.py already reuses summary.format() for per-camera sections.

7. **`palletscan/events/dedup.py:74` — reemits counter provably always equals cross_camera_merges.** Both are incremented only together at the single merge site (dedup.py:109-110); no consumer treats them as distinct (station summary and dashboard JS read only cross_camera_merges; tests assert them equal). Drop the field and serve `"reemits": self.cross_camera_merges` in stats() to keep the pinned dict shape (test_dedup.py:283), or update that one test.

---

## Refuted during verification (recorded so they aren't re-raised)

- **SQLite WAL-pragma lock race (`sinks.py:131`):** claimed the first WAL conversion could raise 'database is locked' before busy_timeout is set, dropping an event. Refuted empirically: sqlite3.connect's default timeout=5.0 installs a busy handler before any pragma, and ReadStore's per-call connect + fetchall pattern holds shared locks only for milliseconds.
- **Report window treated as UTC (`reporting/ab.py:37`):** the naive-as-UTC behavior is real but deliberate, documented in the docstring, pinned by test_ab_window_accepts_naive_bounds_as_utc, and recorded as ASSUMPTIONS #49a; no shipped input surface sends local wall-clock window bounds.
