/* Regression tests for dashboard-frontend findings 6, 7, 9, 10, 11, 14 of
 * REVIEW_7e4c22c.md, each derived from the review's reproduction scenario.
 *
 * Run all:      node tests/js/app_js_tests.mjs
 * Run one:      node tests/js/app_js_tests.mjs test_f6_poll_preserves_note_drafts
 * Pre-fix red:  APP_JS_PATH=/tmp/app_orig.js node tests/js/app_js_tests.mjs
 */
"use strict";

import assert from "node:assert/strict";

import {
  createSandbox,
  errorResponse,
  findFirst,
  flush,
  jsonResponse,
  routeFetch,
  textOf,
} from "./harness.mjs";

/* -- fixtures ------------------------------------------------------------------ */

function makeMiss(overrides = {}) {
  return {
    event_id: "m1",
    candidate_id: "cand-1",
    source_id: "camA",
    wall_time_iso: "2026-06-12T00:00:00+00:00",
    images: [],
    reviewed: false,
    review_note: "",
    reviewed_utc: null,
    ...overrides,
  };
}

const AB_REPORT = {
  cameras: {
    camA: {
      passes_seen: 1,
      passes_decoded: 1,
      read_rate: 1,
      ttfd_median_s: 0.1,
      ttfd_p95_s: 0.2,
      decodes_per_pass: 1,
      misses: 0,
    },
  },
  business: { passes: 1, misses: 0 },
  window: { from: null, to: null },
};

const RECONCILIATION = {
  expected: 2,
  matched: ["a"],
  missing: ["b"],
  unexpected: [],
  true_read_rate: 0.5,
};

function noteInputIn(grid) {
  return findFirst(
    grid,
    (n) => n.tagName === "INPUT" && n.placeholder === "review note"
  );
}

/* -- finding 6: poll loop destroys in-progress review notes ----------------------- */

async function test_f6_poll_preserves_note_drafts() {
  const routes = { "/api/misses": jsonResponse([makeMiss()]) };
  const { ctx, document, byId } = await createSandbox({
    fetch: routeFetch(routes),
  });
  const grid = byId.get("misses");

  await ctx.refreshMisses();
  const input = noteInputIn(grid);
  assert.ok(input, "miss card should contain a review-note input");

  // Operator types mid-poll: value + input event + focus.
  input.value = "checking pallet wrap";
  if (input.oninput) input.oninput();
  document.activeElement = input;

  await ctx.refreshMisses(); // the ~6 s poll
  const visible = noteInputIn(grid);
  assert.ok(visible, "miss card should still contain a review-note input");
  assert.ok(
    visible === input,
    "focused poll rebuilt the card: the visible input is not the very element " +
      "the operator is typing into (cursor/selection would be yanked)"
  );
  assert.equal(
    visible.value,
    "checking pallet wrap",
    "poll wiped the in-progress review note while the input was focused"
  );

  // Blurred: the rebuild may proceed, but the draft cache must repopulate it.
  document.activeElement = null;
  await ctx.refreshMisses();
  const rebuilt = noteInputIn(grid);
  assert.ok(rebuilt, "rebuilt miss card should contain a review-note input");
  assert.equal(
    rebuilt.value,
    "checking pallet wrap",
    "draft text was not repopulated into the rebuilt card (draft-map path)"
  );

  // Save-vs-typing race: text typed while the review POST is in flight must
  // survive the save's draft cleanup (the cleanup may only drop the draft if
  // it still equals exactly what that save sent).
  {
    let resolvePost;
    const postGate = new Promise((resolve) => {
      resolvePost = resolve;
    });
    const serverMiss = makeMiss({ review_note: "" });
    const routes = {
      "/api/misses": () => jsonResponse([serverMiss]),
      "/api/misses/m1/review": () => postGate,
    };
    const { ctx, document, byId } = await createSandbox({
      fetch: routeFetch(routes),
    });
    const grid = byId.get("misses");
    await ctx.refreshMisses();
    const input = noteInputIn(grid);
    assert.ok(input, "miss card should contain a review-note input");
    const btn = findFirst(grid, (n) => n.tagName === "BUTTON");
    assert.ok(btn, "miss card should contain the review button");

    input.value = "abc";
    input.oninput();
    document.activeElement = input;
    btn.onclick(); // review POST of "abc" now in flight
    input.value = "abcdef"; // operator keeps typing while it is in flight
    input.oninput();

    // Server acknowledges the save of "abc"; subsequent GETs reflect it.
    serverMiss.reviewed = true;
    serverMiss.review_note = "abc";
    resolvePost(jsonResponse({ status: "ok" }));
    await flush();

    // Operator blurs; the next poll rebuild must keep the newer draft, not
    // reset the input to the server's older "abc".
    document.activeElement = null;
    await ctx.refreshMisses();
    const rebuiltRace = noteInputIn(grid);
    assert.ok(rebuiltRace, "rebuilt miss card should contain a review-note input");
    assert.equal(
      rebuiltRace.value,
      "abcdef",
      "text typed during the in-flight save was discarded by the save's " +
        "unconditional draft cleanup (silent loss of the trailing keystrokes)"
    );
  }
}

/* -- finding 7: failed review POST is silently dropped ------------------------------ */

async function test_f7_failed_review_surfaces_error() {
  // Case 1: HTTP 500 on the review POST.
  {
    const routes = {
      "/api/misses": jsonResponse([makeMiss()]),
      "/api/misses/m1/review": errorResponse(500),
    };
    const { ctx, byId } = await createSandbox({ fetch: routeFetch(routes) });
    const grid = byId.get("misses");
    await ctx.refreshMisses();
    const btn = findFirst(grid, (n) => n.tagName === "BUTTON");
    assert.ok(btn, "miss card should contain the review button");

    btn.onclick(); // browser discards the handler's return value
    await flush();

    const cardText = textOf(grid);
    assert.ok(
      /failed|500/.test(cardText),
      `failed review POST (500) produced no visible error; card text: "${cardText}"`
    );

    // The ~6 s poll rebuilds the card (blurred): the failure notice must
    // survive the rebuild, not vanish leaving the miss looking unreviewed.
    await ctx.refreshMisses();
    const rebuiltText = textOf(grid);
    assert.ok(
      /save failed/.test(rebuiltText),
      `poll rebuild wiped the save-failed notice; card text now: "${rebuiltText}"`
    );
  }

  // Case 2: network-level rejection must land in the same catch — no
  // unhandled rejection — and still surface in the card.
  {
    const unhandled = [];
    const trap = (err) => unhandled.push(err);
    process.on("unhandledRejection", trap);
    try {
      const routes = {
        "/api/misses": jsonResponse([makeMiss()]),
        "/api/misses/m1/review": new Error("connection refused"),
      };
      const { ctx, byId } = await createSandbox({ fetch: routeFetch(routes) });
      const grid = byId.get("misses");
      await ctx.refreshMisses();
      const btn = findFirst(grid, (n) => n.tagName === "BUTTON");
      assert.ok(btn, "miss card should contain the review button");

      btn.onclick();
      await flush();

      assert.equal(
        unhandled.length,
        0,
        `network failure of the review POST produced an unhandled rejection: ${unhandled[0]}`
      );
      const cardText = textOf(grid);
      assert.ok(
        /failed/.test(cardText),
        `network-failed review POST produced no visible error; card text: "${cardText}"`
      );
    } finally {
      process.removeListener("unhandledRejection", trap);
    }
  }

  // Case 3: the review POST SUCCEEDS but the follow-up GET /api/misses fails
  // once. A refresh failure must never be labeled a save failure, and the
  // saved draft must still be cleaned up.
  {
    let failNextGet = false;
    const serverMiss = makeMiss({ review_note: "server note" });
    const routes = {
      "/api/misses": () => {
        if (failNextGet) {
          failNextGet = false;
          return new Error("connection refused");
        }
        return jsonResponse([serverMiss]);
      },
      "/api/misses/m1/review": jsonResponse({ status: "ok" }),
    };
    const { ctx, byId } = await createSandbox({ fetch: routeFetch(routes) });
    const grid = byId.get("misses");
    await ctx.refreshMisses();
    const input = noteInputIn(grid);
    assert.ok(input, "miss card should contain a review-note input");
    const btn = findFirst(grid, (n) => n.tagName === "BUTTON");
    assert.ok(btn, "miss card should contain the review button");
    input.value = "typed draft";
    input.oninput();

    failNextGet = true; // the refresh right after the save will fail
    btn.onclick();
    await flush();

    const cardText = textOf(grid);
    assert.ok(
      !/save failed/.test(cardText),
      "transient refresh failure after a SUCCESSFUL save was mislabeled a " +
        `save failure; card text: "${cardText}"`
    );

    // The save went through, so the draft must be gone: the next successful
    // rebuild shows the server's note, not the stale draft.
    await ctx.refreshMisses();
    const rebuilt = noteInputIn(grid);
    assert.ok(rebuilt, "rebuilt miss card should contain a review-note input");
    assert.equal(
      rebuilt.value,
      "server note",
      "draft was not cleaned up after a successful save whose follow-up " +
        "refresh failed (cleanup must not ride on the refresh)"
    );
  }
}

/* -- finding 9: transient report errors must not claim "no manifest loaded" --------- */

async function test_f9_transient_report_error_keeps_panel() {
  const routes = {
    "/api/report/ab": jsonResponse(AB_REPORT),
    "/api/report/reconciliation": jsonResponse(RECONCILIATION),
  };
  const { ctx, byId } = await createSandbox({ fetch: routeFetch(routes) });
  const rec = byId.get("reconciliation");
  const status = byId.get("report-status");

  await ctx.refreshReport();
  assert.ok(
    textOf(rec).includes("expected 2"),
    `reconciliation panel not populated after successful refresh: "${textOf(rec)}"`
  );

  // Transient failure of BOTH report endpoints: stale data must stay visible.
  routes["/api/report/ab"] = errorResponse(500);
  routes["/api/report/reconciliation"] = errorResponse(500);
  await ctx.refreshReport();
  const recText = textOf(rec);
  assert.ok(
    recText.includes("expected 2"),
    `transient 500 wiped the reconciliation panel; panel now: "${recText}"`
  );
  assert.ok(
    !recText.includes("no manifest loaded"),
    'transient 500 was misreported as "no manifest loaded"'
  );
  assert.ok(
    /failed/.test(status.textContent),
    `transient report failure not surfaced in #report-status: "${status.textContent}"`
  );

  // 404 is the genuine no-manifest signal and must still render the message.
  routes["/api/report/ab"] = jsonResponse(AB_REPORT);
  routes["/api/report/reconciliation"] = errorResponse(404);
  await ctx.refreshReport();
  assert.ok(
    textOf(rec).includes("no manifest loaded"),
    `404 should render "no manifest loaded"; panel: "${textOf(rec)}"`
  );
  assert.equal(
    status.textContent,
    "",
    "report status note should clear once the refresh succeeds (404 is handled, not stale)"
  );
}

/* -- finding 10: one MJPEG error permanently kills the live tile --------------------- */

async function test_f10_live_tile_reconnects_after_error() {
  const { ctx, timers, byId } = await createSandbox();
  ctx.buildLiveGrid(["camA"]);
  const grid = byId.get("live-grid");
  const card = grid.children[0];
  assert.ok(card, "live grid should contain a card for camA");
  const img = findFirst(card, (n) => n.tagName === "IMG");
  assert.ok(img, "live card should contain the MJPEG img");

  const timersBefore = timers.length;
  img.onerror();
  assert.ok(
    card.contains(img),
    "img was removed from the live card on first stream error (tile killed)"
  );
  assert.ok(
    timers.length > timersBefore,
    "no reconnect timer scheduled after the stream error"
  );

  timers[timers.length - 1].fn();
  assert.ok(
    img.src.includes("retry=1"),
    `reconnect did not re-set img.src with a cache-buster; src: "${img.src}"`
  );

  // A second failure must retry again — no permanent death.
  const timersMid = timers.length;
  img.onerror();
  assert.ok(card.contains(img), "img was removed on the second stream error");
  assert.ok(timers.length > timersMid, "no reconnect timer after second error");
  timers[timers.length - 1].fn();
  assert.ok(
    img.src.includes("retry=2"),
    `second reconnect should increment the cache-buster; src: "${img.src}"`
  );
}

/* -- finding 11: client must not pre-decode the manifest (mangles non-UTF-8) ---------- */

async function test_f11_upload_sends_raw_bytes() {
  // Blob.text() decodes with U+FFFD replacement and never throws, so a cp1252
  // Excel export would reach the server as "valid" mangled UTF-8 and bypass
  // its strict-decode 400. The upload must pass the File through untouched.
  const file = {
    name: "manifest.csv",
    text: () => {
      throw new Error("must not client-decode");
    },
  };
  let capturedBody = null;
  const routes = {
    "/api/manifest": (url, opts) => {
      capturedBody = opts.body;
      return {
        ok: false,
        status: 400,
        json: async () => ({
          detail:
            "manifest must be UTF-8 text (CSV); body is not: invalid start byte",
        }),
      };
    },
  };
  const { ctx, byId } = await createSandbox({ fetch: routeFetch(routes) });
  byId.get("manifest-file").files = [file];
  const status = byId.get("manifest-status");

  await ctx.uploadManifest();

  assert.ok(
    capturedBody === file,
    "upload must send the raw File so the server's strict UTF-8 decode is " +
      `the single authority; body sent: ${String(capturedBody)}`
  );
  assert.ok(
    status.textContent.includes("upload failed: 400"),
    `status should name the HTTP status; got: "${status.textContent}"`
  );
  assert.ok(
    status.textContent.includes("manifest must be UTF-8"),
    `status should surface the server's rejection detail; got: "${status.textContent}"`
  );
}

/* -- finding 14: manifest upload gives no feedback on network failure ----------------- */

async function test_f14_manifest_upload_network_failure_feedback() {
  const routes = { "/api/manifest": new Error("connection refused") };
  const { ctx, byId } = await createSandbox({ fetch: routeFetch(routes) });
  const status = byId.get("manifest-status");
  status.textContent = "stored 7 payloads"; // stale text from a previous upload
  // uploadManifest sends the File object straight through (finding 11), so
  // the stub needs no .text(); the body never gets client-decoded.
  byId.get("manifest-file").files = [{ name: "manifest.csv" }];

  try {
    await ctx.uploadManifest();
  } catch (err) {
    // pre-fix app.js rejects right through the onclick; the assertion below
    // reports the actual symptom (stale status text).
  }
  assert.ok(
    status.textContent.startsWith("upload failed"),
    `network-failed upload left status text: "${status.textContent}" ` +
      '(expected it to start with "upload failed")'
  );
}

/* -- runner ----------------------------------------------------------------------- */

const TESTS = {
  test_f6_poll_preserves_note_drafts,
  test_f7_failed_review_surfaces_error,
  test_f9_transient_report_error_keeps_panel,
  test_f10_live_tile_reconnects_after_error,
  test_f11_upload_sends_raw_bytes,
  test_f14_manifest_upload_network_failure_feedback,
};

async function main() {
  const requested = process.argv[2];
  let names;
  if (requested) {
    if (!TESTS[requested]) {
      console.error(
        `unknown test "${requested}"; available: ${Object.keys(TESTS).join(", ")}`
      );
      process.exit(2);
    }
    names = [requested];
  } else {
    names = Object.keys(TESTS);
  }

  let failures = 0;
  for (const name of names) {
    try {
      await TESTS[name]();
      console.log(`PASS ${name}`);
    } catch (err) {
      failures += 1;
      console.error(`FAIL ${name}`);
      console.error(err && err.stack ? err.stack : String(err));
    }
  }
  if (failures) {
    console.error(`${failures} of ${names.length} test(s) failed`);
    process.exit(1);
  }
  console.log(`${names.length} test(s) passed`);
}

await main();
