/* Minimal node:vm harness for the PalletScan dashboard frontend.
 *
 * Loads the REAL palletscan/web/static/app.js (no build step, vanilla JS)
 * into a vm context with a tiny DOM stub whose element registry is derived
 * from the REAL index.html, so the stub cannot drift from the page. Top-level
 * `function` declarations in app.js land on the context global, so tests call
 * ctx.refreshMisses(), ctx.uploadManifest(), etc. directly.
 *
 * APP_JS_PATH env var overrides the app.js path (used to prove each
 * regression test goes red against the pre-fix source).
 */
"use strict";

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "..", "..");
export const APP_JS_PATH =
  process.env.APP_JS_PATH ||
  path.join(REPO_ROOT, "palletscan", "web", "static", "app.js");
export const INDEX_HTML_PATH = path.join(
  REPO_ROOT,
  "palletscan",
  "web",
  "static",
  "index.html"
);

/* -- DOM stub ---------------------------------------------------------------- */

export class Element {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.type = "";
    this.placeholder = "";
    this.src = "";
    this.alt = "";
    this.href = "";
    this.target = "";
    this.loading = "";
    this.onclick = null;
    this.onerror = null;
    this.onload = null;
    this.onchange = null;
    this.oninput = null;
    this.files = [];
    this.children = [];
    this.parent = null;
  }

  appendChild(node) {
    node.parent = this;
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    for (const node of nodes) this.appendChild(node);
  }

  replaceWith(node) {
    if (!this.parent) return;
    const idx = this.parent.children.indexOf(this);
    node.parent = this.parent;
    this.parent.children[idx] = node;
    this.parent = null;
  }

  contains(node) {
    if (!node) return false;
    if (node === this) return true;
    return this.children.some((child) => child.contains(node));
  }
}

/* -- registry: ids from the real index.html ----------------------------------- */

function parseIndexIds(html) {
  const ids = new Map(); // id -> tag name
  for (const m of html.matchAll(/<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*\bid="([^"]+)"/g)) {
    ids.set(m[2], m[1]);
  }
  return ids;
}

function parseQueriedIds(appSrc) {
  // Matches $("#id"), $(`#id`), document.querySelector("#id"), and the
  // compound "#events tbody" (captures "events").
  const ids = new Set();
  for (const m of appSrc.matchAll(/(?:\$|querySelector)\(\s*["'`]#([A-Za-z0-9_-]+)/g)) {
    ids.add(m[1]);
  }
  return ids;
}

/** Assert the stub registry covers every id app.js queries, and that the
 * registry comes from the real index.html (it is parsed from it, so any
 * missing id means app.js and index.html drifted apart). */
export function verifyRegistry() {
  const appSrc = fs.readFileSync(APP_JS_PATH, "utf8");
  const html = fs.readFileSync(INDEX_HTML_PATH, "utf8");
  const registered = parseIndexIds(html);
  for (const id of parseQueriedIds(appSrc)) {
    if (!registered.has(id)) {
      throw new Error(
        `app.js queries #${id} but index.html (${INDEX_HTML_PATH}) defines no such id`
      );
    }
  }
  // Cross-check: every registered id must literally appear in the real page.
  for (const id of registered.keys()) {
    if (!html.includes(`id="${id}"`)) {
      throw new Error(`stub registry drifted: id "${id}" not in index.html`);
    }
  }
  return registered;
}

/* -- sandbox ------------------------------------------------------------------ */

/** Wait out pending promise chains (bootstrap loop, fired handlers). */
export async function flush(rounds = 20) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/**
 * Create a fresh sandbox and execute the real app.js in it.
 *
 * @param {object} [opts]
 * @param {Function} [opts.fetch] test fetch stub; defaults to rejecting
 *   (every app.js caller of the bootstrap poll handles rejection).
 * @returns {{ctx, document, timers, byId, fetchCalls}}
 */
export async function createSandbox({ fetch: fetchImpl } = {}) {
  const registered = verifyRegistry();

  const byId = new Map();
  for (const [id, tag] of registered) byId.set(id, new Element(tag));
  // app.js queries "#events tbody": pre-register a tbody under #events.
  const eventsTbody = new Element("tbody");
  byId.get("events").appendChild(eventsTbody);

  const document = {
    activeElement: null,
    createElement: (tag) => new Element(tag),
    querySelector: (sel) => {
      if (sel === "#events tbody") return eventsTbody;
      if (typeof sel === "string" && sel.startsWith("#") && !sel.includes(" ")) {
        return byId.get(sel.slice(1)) || null;
      }
      throw new Error(`DOM stub does not support selector: ${sel}`);
    },
  };

  const timers = []; // {fn, ms} — captured, never scheduled
  const fetchCalls = [];
  const defaultFetch = async (url) => {
    throw new Error(`no fetch stub for ${url}`);
  };
  const sandbox = {
    document,
    console,
    fetch: (...args) => {
      fetchCalls.push(args);
      return (fetchImpl || defaultFetch)(...args);
    },
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout: () => {},
  };
  const ctx = vm.createContext(sandbox);
  const src = fs.readFileSync(APP_JS_PATH, "utf8");
  vm.runInContext(src, ctx, { filename: APP_JS_PATH });
  await flush(); // let the bootstrap loop()'s first pass settle
  return { ctx, document, timers, byId, fetchCalls };
}

/* -- tree helpers -------------------------------------------------------------- */

/** All textContent in the subtree, space-joined. */
export function textOf(node) {
  let out = node.textContent ? String(node.textContent) : "";
  for (const child of node.children) {
    const t = textOf(child);
    if (t) out += (out ? " " : "") + t;
  }
  return out;
}

/** First node in the subtree matching pred, or null. */
export function findFirst(node, pred) {
  if (pred(node)) return node;
  for (const child of node.children) {
    const hit = findFirst(child, pred);
    if (hit) return hit;
  }
  return null;
}

/* -- fetch stub helpers ---------------------------------------------------------- */

export function jsonResponse(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

export function errorResponse(status) {
  return { ok: false, status, json: async () => ({ detail: `HTTP ${status}` }) };
}

/**
 * Fetch stub routed by pathname (query string stripped). Route values may be
 * response objects, Errors (network-level rejection), or (url, opts) => value
 * functions. Mutate `routes` mid-test to change behaviour between calls.
 */
export function routeFetch(routes) {
  return async (url, opts = {}) => {
    const key = String(url).split("?")[0];
    const route = routes[key];
    if (route === undefined) throw new Error(`unstubbed fetch: ${url}`);
    const value = typeof route === "function" ? route(url, opts) : route;
    if (value instanceof Error) throw value;
    return value;
  };
}
