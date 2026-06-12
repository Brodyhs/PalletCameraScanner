/* PalletScan dashboard: vanilla JS polling, no build step, offline-first. */
"use strict";

const POLL_MS = 2000;
const $ = (sel) => document.querySelector(sel);

let liveGridBuilt = false;

function fmt(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(digits);
  }
  return String(value);
}

function pct(value) {
  return value === null || value === undefined
    ? "—"
    : (100 * value).toFixed(1) + "%";
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function tile(label, value, cls) {
  const box = el("div", "tile" + (cls ? " " + cls : ""));
  box.appendChild(el("div", "tile-value", value));
  box.appendChild(el("div", "tile-label", label));
  return box;
}

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url}: ${resp.status}`);
  return resp.json();
}

/* -- live views ------------------------------------------------------------ */

function buildLiveGrid(cameraIds) {
  const grid = $("#live-grid");
  grid.replaceChildren();
  if (!cameraIds.length) {
    $("#mode-note").textContent = "standalone (read-only, no live runners)";
    $("#live-section").hidden = true;
    return;
  }
  for (const id of cameraIds) {
    const card = el("div", "live-card");
    card.appendChild(el("h3", null, id));
    const img = el("img");
    img.src = `/live/${encodeURIComponent(id)}`;
    img.alt = `live view: ${id}`;
    img.onerror = () => {
      img.replaceWith(el("p", "muted", "live view unavailable"));
    };
    card.appendChild(img);
    grid.appendChild(card);
  }
}

/* -- stats tiles ------------------------------------------------------------ */

function cameraCard(id, snap) {
  const card = el("div", "camera-card");
  card.appendChild(el("h3", null, id));
  const grid = el("div", "tile-grid");
  grid.appendChild(tile("read rate 1h", pct(snap.read_rate_1h)));
  grid.appendChild(tile("read rate 24h", pct(snap.read_rate_24h)));
  grid.appendChild(tile("passes/hour", fmt(snap.passes.per_hour, 1)));
  grid.appendChild(tile("fps", fmt(snap.fps, 1)));
  grid.appendChild(tile("passes", fmt(snap.passes.emitted)));
  grid.appendChild(
    tile("misses", fmt(snap.misses.emitted), snap.misses.emitted ? "warn" : "")
  );
  grid.appendChild(tile("decode p50 ms", fmt(snap.decode.p50_ms, 1)));
  grid.appendChild(tile("decode p95 ms", fmt(snap.decode.p95_ms, 1)));
  const queues = Object.entries(snap.queues || {})
    .map(([name, depth]) => `${name}:${depth}`)
    .join(" ");
  grid.appendChild(tile("queues", queues || "—"));
  const src = snap.source || {};
  grid.appendChild(
    tile(
      "stalls / reconnects",
      `${fmt(src.stalls)} / ${fmt(src.reconnects)}`,
      src.stalls ? "warn" : ""
    )
  );
  grid.appendChild(tile("uptime s", fmt(snap.uptime_s, 0)));
  if (snap.outbox) {
    grid.appendChild(tile("outbox depth", fmt(snap.outbox.depth)));
  }
  card.appendChild(grid);
  return card;
}

function renderStats(stats) {
  $("#generated").textContent = stats.generated_utc;
  const cameraIds = Object.keys(stats.cameras);
  if (!liveGridBuilt) {
    buildLiveGrid(cameraIds);
    liveGridBuilt = true;
  }
  const tiles = $("#camera-tiles");
  tiles.replaceChildren();
  for (const id of cameraIds) tiles.appendChild(cameraCard(id, stats.cameras[id]));
  const business = $("#business-section");
  if (stats.business) {
    business.hidden = false;
    const grid = $("#business-tiles");
    grid.replaceChildren();
    grid.appendChild(tile("business passes", fmt(stats.business.passes_emitted)));
    grid.appendChild(
      tile("cross-camera merges", fmt(stats.business.cross_camera_merges))
    );
    grid.appendChild(
      tile("repeats suppressed", fmt(stats.business.repeats_suppressed))
    );
    grid.appendChild(tile("misses forwarded", fmt(stats.business.misses_forwarded)));
  } else {
    business.hidden = true;
  }
}

/* -- events table ------------------------------------------------------------ */

function renderEvents(events) {
  const body = $("#events tbody");
  body.replaceChildren();
  for (const ev of events) {
    const row = el("tr", ev.kind === "miss" ? "miss-row" : "");
    row.appendChild(el("td", null, (ev.wall_time_iso || "").slice(0, 19)));
    row.appendChild(el("td", null, ev.kind));
    row.appendChild(el("td", null, ev.payload || ev.candidate_id || ""));
    const cams = ev.cameras
      ? Object.entries(ev.cameras).map(([c, n]) => `${c}(${n})`).join(" ")
      : ev.source_id || "";
    row.appendChild(el("td", null, cams));
    row.appendChild(el("td", null, fmt(ev.decode_count ?? "")));
    row.appendChild(el("td", null, ev.symbology || ""));
    body.appendChild(row);
  }
}

/* -- miss gallery ------------------------------------------------------------ */

async function markReviewed(eventId, reviewed, note) {
  await fetch(`/api/misses/${encodeURIComponent(eventId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reviewed, note: note || null }),
  });
  await refreshMisses();
}

function missCard(miss) {
  const card = el("div", "miss-card" + (miss.reviewed ? " reviewed" : ""));
  const head = el("div", "miss-head");
  head.appendChild(el("strong", null, miss.candidate_id));
  head.appendChild(el("span", "muted", ` ${miss.source_id} `));
  head.appendChild(el("span", "muted", (miss.wall_time_iso || "").slice(0, 19)));
  card.appendChild(head);
  const strip = el("div", "thumb-strip");
  if (miss.images && miss.images.length) {
    for (const url of miss.images.slice(0, 8)) {
      const link = el("a");
      link.href = url;
      link.target = "_blank";
      const img = el("img");
      img.src = url;
      img.loading = "lazy";
      link.appendChild(img);
      strip.appendChild(link);
    }
  } else {
    strip.appendChild(el("p", "muted", "evidence pruned"));
  }
  card.appendChild(strip);
  const controls = el("div", "miss-controls");
  const note = el("input");
  note.type = "text";
  note.placeholder = "review note";
  note.value = miss.review_note || "";
  controls.appendChild(note);
  const btn = el("button", null, miss.reviewed ? "mark unreviewed" : "mark reviewed");
  btn.onclick = () => markReviewed(miss.event_id, !miss.reviewed, note.value);
  controls.appendChild(btn);
  if (miss.reviewed && miss.reviewed_utc) {
    controls.appendChild(
      el("span", "muted", `reviewed ${miss.reviewed_utc.slice(0, 19)}`)
    );
  }
  card.appendChild(controls);
  return card;
}

async function refreshMisses() {
  const unreviewedOnly = $("#unreviewed-only").checked;
  const misses = await getJSON(`/api/misses?unreviewed_only=${unreviewedOnly}`);
  const grid = $("#misses");
  grid.replaceChildren();
  if (!misses.length) {
    grid.appendChild(el("p", "muted", "no misses"));
    return;
  }
  for (const miss of misses) grid.appendChild(missCard(miss));
}

/* -- A/B report + reconciliation ---------------------------------------------- */

function renderReport(report) {
  const target = $("#report");
  target.replaceChildren();
  const cameras = Object.keys(report.cameras || {});
  if (!cameras.length) {
    target.appendChild(el("p", "muted", "no pass data yet"));
    return;
  }
  const table = el("table");
  const head = el("tr");
  head.appendChild(el("th", null, "metric"));
  for (const cam of cameras) head.appendChild(el("th", null, cam));
  table.appendChild(head);
  const rows = [
    ["passes seen", (c) => fmt(c.passes_seen)],
    ["passes decoded", (c) => fmt(c.passes_decoded)],
    ["read rate", (c) => pct(c.read_rate)],
    ["ttfd median s", (c) => fmt(c.ttfd_median_s, 3)],
    ["ttfd p95 s", (c) => fmt(c.ttfd_p95_s, 3)],
    ["decodes/pass", (c) => fmt(c.decodes_per_pass, 2)],
    ["misses", (c) => fmt(c.misses)],
  ];
  for (const [label, render] of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", null, label));
    for (const cam of cameras) tr.appendChild(el("td", null, render(report.cameras[cam])));
    table.appendChild(tr);
  }
  target.appendChild(table);
  target.appendChild(
    el(
      "p",
      "muted",
      `business passes ${report.business.passes} · misses ${report.business.misses} · window ${report.window.from || "start"} → ${report.window.to || "now"}`
    )
  );
}

function renderReconciliation(rec) {
  const target = $("#reconciliation");
  target.replaceChildren();
  if (!rec.expected) {
    target.appendChild(el("p", "muted", "no manifest loaded"));
    return;
  }
  const summary = el("p");
  summary.textContent =
    `expected ${rec.expected} · matched ${rec.matched.length} · ` +
    `missing ${rec.missing.length} · unexpected ${rec.unexpected.length} · ` +
    `true read rate ${pct(rec.true_read_rate)}`;
  target.appendChild(summary);
  if (rec.missing.length) {
    target.appendChild(
      el("p", "warn-text", "missing: " + rec.missing.slice(0, 20).join(", "))
    );
  }
  if (rec.unexpected.length) {
    target.appendChild(
      el("p", "muted", "unexpected: " + rec.unexpected.slice(0, 20).join(", "))
    );
  }
}

async function uploadManifest() {
  const input = $("#manifest-file");
  const status = $("#manifest-status");
  if (!input.files.length) {
    status.textContent = "choose a CSV first";
    return;
  }
  const text = await input.files[0].text();
  const resp = await fetch("/api/manifest", {
    method: "POST",
    headers: { "Content-Type": "text/csv" },
    body: text,
  });
  if (resp.ok) {
    const out = await resp.json();
    status.textContent = `stored ${out.stored} payloads`;
    await refreshReport();
  } else {
    status.textContent = `upload failed: ${resp.status}`;
  }
}

async function refreshReport() {
  try {
    renderReport(await getJSON("/api/report/ab"));
  } catch (err) {
    /* report endpoints optional while no data */
  }
  try {
    renderReconciliation(await getJSON("/api/report/reconciliation"));
  } catch (err) {
    $("#reconciliation").replaceChildren(el("p", "muted", "no manifest loaded"));
  }
}

/* -- poll loop ------------------------------------------------------------ */

async function tick() {
  try {
    renderStats(await getJSON("/stats.json"));
    renderEvents(await getJSON("/api/events?limit=50"));
  } catch (err) {
    $("#generated").textContent = `connection lost (${err.message})`;
  }
}

let slowTick = 0;
async function loop() {
  await tick();
  if (slowTick % 3 === 0) {
    await refreshMisses().catch(() => {});
    await refreshReport();
  }
  slowTick += 1;
  setTimeout(loop, POLL_MS);
}

$("#manifest-upload").onclick = uploadManifest;
$("#unreviewed-only").onchange = () => refreshMisses().catch(() => {});
loop();
