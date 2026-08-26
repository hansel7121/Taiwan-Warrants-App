// Shared: Supabase auth, fetch wrapper, tab nav + view persistence, table helpers.

// --- Supabase auth bootstrap -------------------------------------------

const SUPABASE_URL = window.SUPABASE_URL;

const SUPABASE_ANON_KEY = window.SUPABASE_ANON_KEY;
// Local redundancy instance: no login, no Supabase client, no /login redirects.

const LOCAL_MODE = window.LOCAL_MODE;
// window.supabase is undefined when the CDN script was blocked (ad-blocker);
// _boot() then shows a full-page error instead of letting the script crash.

let _sb = null;

if (!LOCAL_MODE && SUPABASE_URL && window.supabase) {
  _sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
}

function _logout() {
  if (_sb) _sb.auth.signOut();
  else location.replace("/login");
}

// fetch() wrapper that injects the Supabase bearer token and handles
// auth failures. Falls back to plain fetch when auth is not configured.

async function api(url, opts) {
  // Local mode: no bearer token, no 401/403 handling — the local server
  // never challenges auth.
  if (LOCAL_MODE || !_sb) return fetch(url, opts);
  const { data } = await _sb.auth.getSession();
  const token = data.session && data.session.access_token;
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(url, Object.assign({}, opts, { headers }));
  if (res.status === 401) { location.replace("/login"); throw new Error("unauthorized"); }
  if (res.status === 403) {
    document.body.innerHTML =
      '<div style="max-width:420px;margin:15vh auto;text-align:center;font-family:inherit;color:#d8dee4">' +
      '<h2 style="font-size:18px;margin-bottom:10px">Your account is not approved yet</h2>' +
      '<p style="color:#7b8794;font-size:13px">Ask the administrator to add your email to the allow-list.</p>' +
      '<p style="margin-top:16px"><a href="#" onclick="_logout();return false" style="color:#e0a137">Sign out</a></p></div>';
    throw new Error("not_allowed");
  }
  return res;
}

// api() + JSON, raising on any non-2xx instead of handing back an error page.
// api() only redirects/throws for 401/403, so a 500 would otherwise reach
// res.json() as Flask's HTML error page and fail with the opaque
// "Unexpected token '<'". Surfaces the server's {"error": ...} when there is
// one, the status line otherwise.

async function apiJson(url, opts) {
  const res = await api(url, opts);
  const body = await res.text();
  if (!res.ok) {
    let detail = "";
    try { detail = (JSON.parse(body) || {}).error || ""; } catch (e) {}
    throw new Error(detail || `${res.status} ${res.statusText || "request failed"}`);
  }
  return body ? JSON.parse(body) : null;
}

// "updated N min ago" suffix for scanner status lines, from the
// as_of/cached fields the backend attaches to cached market data.

function asOfLabel(data) {
  if (!data || !data.as_of) return "";
  const mins = Math.max(0, Math.round((Date.now() - new Date(data.as_of).getTime()) / 60000));
  const age = mins === 0 ? "updated just now" : `updated ${mins} min ago`;
  return ` · ${age}${data.cached === false ? " (live fetch)" : ""}`;
}

// Live-ticking "updated N min ago". A fetch handler sets the status via
// setStatusWithAge, which remembers the count prefix + the data object per
// scanner. A single interval then re-renders just the age suffix every 30s,
// so the minute count climbs without re-fetching. Keyed by scanner name.

window._lastAsOf = window._lastAsOf || {};

function setStatusWithAge(key, elId, base, data) {
  window._lastAsOf[key] = { elId, base, data };
  const el = document.getElementById(elId);
  if (el) el.textContent = base + asOfLabel(data);
}

function _tickAges() {
  for (const key in window._lastAsOf) {
    const rec = window._lastAsOf[key];
    if (!rec || !rec.data) continue;
    const el = document.getElementById(rec.elId);
    // Skip missing or hidden (inactive-tab) status lines, and never clobber a
    // transient message (e.g. "Fetching…") that replaced the count line.
    if (!el || el.offsetParent === null) continue;
    if (!el.textContent.startsWith(rec.base)) continue;
    el.textContent = rec.base + asOfLabel(rec.data);
  }
}

setInterval(_tickAges, 30000);

// "Sync now" helper shared by all three scanner tabs. The /sync_X route is
// synchronous on the backend (blocks until the scrape+store completes), so
// this is a plain linear sequence — no polling loop: clear the table, await
// the sync, then do exactly one read to render the result. Always re-enables
// the button(s), even on error.
//
// readBtn is also disabled for the duration: the "Read" button isn't wired
// through this function's own request, so without disabling it a manual click
// mid-sync would race its own read against onDone()'s trailing read, and
// whichever response landed last would win the render.

async function refreshNow(kind, statusEl, btn, onDone, tableEl, readBtn) {
  if (!statusEl || !btn) return;
  const origLabel = btn.textContent;
  const wasDisabled = btn.disabled;
  const readWasDisabled = readBtn ? readBtn.disabled : null;
  btn.disabled = true;
  btn.textContent = "Syncing…";
  if (readBtn) readBtn.disabled = true;
  statusEl.textContent = "Syncing market data… this may take a bit";
  if (tableEl) tableEl.innerHTML = "";
  try {
    const routeMap = { warrants: "/sync_warrant", tw_options: "/sync_tw_option", us_options: "/sync_us_option" };
    const res = await api(routeMap[kind], {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    if (data && Array.isArray(data.skipped) && data.skipped.includes(kind)) {
      statusEl.textContent = "A sync is already running — showing latest.";
    }
    await onDone();
  } catch (e) {
    statusEl.textContent = "Sync failed: " + (e && e.message ? e.message : e);
  } finally {
    btn.disabled = wasDisabled;
    btn.textContent = origLabel;
    if (readBtn) readBtn.disabled = readWasDisabled;
  }
}

function _saveView(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }

function _readView(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }

function switchTab(tab, btn) {
  document.querySelectorAll(".tab-content").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".tab-bar button").forEach(el => el.classList.remove("active"));
  document.getElementById("tab-" + tab).classList.add("active");
  btn.classList.add("active");
  _saveView("ws_activeTab", tab);
}

// Re-apply the tab + options sub-market saved before the last reload. The
// HTML already renders the Options/Taiwan default, so this only acts when a
// different view was active. Runs after auth in _boot.

function restoreView() {
  // Home is the landing tab in admin mode (rendered active in the HTML); user
  // mode lands on Options. Only act when a different tab was saved before the
  // last reload — and only if that tab exists in this mode, so a saved arb tab
  // doesn't strand a user-mode page on nothing.
  const tab = _readView("ws_activeTab");
  if (tab && tab !== "home") {
    const btn = document.querySelector(`.tab-bar button[onclick*="switchTab('${tab}'"]`);
    if (btn && document.getElementById("tab-" + tab)) {
      switchTab(tab, btn);
      if (tab === "portfolio" && typeof loadPortfolioOnce === "function") loadPortfolioOnce();
      if (tab === "live" && typeof loadLiveWarrantOnce === "function") loadLiveWarrantOnce();
      if (tab === "dashboard") loadDashboardOnce();
    }
  }
  // Whatever landed active: if it's Home or Dashboard, populate it now.
  if (document.getElementById("tab-home")?.classList.contains("active")
      && typeof loadHomeOnce === "function") loadHomeOnce();
  if (document.getElementById("tab-dashboard")?.classList.contains("active")) loadDashboardOnce();
  // No Dashboard tab in admin mode — still need watchSet for the scanner stars.
  if (!document.getElementById("tab-dashboard") && can("watchlist")) loadWatchSetOnce();
  if (_readView("ws_optMarket") === "us") {
    const b = document.getElementById("optmkt-btn-us");
    if (b) setOptMarket("us", b);
  }
}

const HIDDEN_COLS = ["warrant_iv", "opt_iv", "tw_option_iv", "us_option_iv", "iv_diff",
  // TW/US raw depth fields — surfaced in the popup panel, not the table.
  "tw_depth_contracts", "tw_fillable", "us_volume", "us_oi"];
// The US Option Match "us_stock_code" value is actually the TW stock code.

const COL_LABELS = { us_stock_code: "tw_stock_code",
                     warrant_depth_lots: "depth (張)", fillable: "fillable?" };

const visCols = (row) => Object.keys(row).filter(c => !HIDDEN_COLS.includes(c));

const colLabel = (c) => COL_LABELS[c] || c;

// Fetch a product-list endpoint; empty array on any failure (network error,
// 401 redirect, etc.) so a populate call just yields an empty <select> rather
// than throwing.
async function fetchProductList(url) {
  try {
    const res = await api(url);
    return await res.json();
  } catch (e) {
    return [];
  }
}

// Fill a <select> with <option>s built from `rows` ({code, name, ...}).
// selectedCodes pre-selects matching options (mirrors a static <option
// selected> from before this change); selectFirst pre-selects the first
// option if nothing else ended up selected (for single-select boxes like
// ivOptProduct that always need something chosen).
// Every product picker gets a type-ahead box. The underlying element stays a
// native <select>, so `selectedOptions`, currentSelection() and the add/remove
// UI are all untouched — the search only decides which <option>s are rendered.
//
// Two rules make filtering safe: the full row list is kept on the element, and
// anything currently selected is always rendered even when it does not match
// the query. Without the second, typing would silently deselect what you had
// already picked, since a removed <option> takes its selection with it.
function populateProductSelect(selectEl, rows, opts) {
  if (!selectEl) return;
  opts = opts || {};
  const labelFn = opts.labelFn || (r => r.name ? `${r.code} ${r.name}` : r.code);

  selectEl._rows = rows;
  selectEl._labelFn = labelFn;
  // Selections live here rather than being read back off the DOM, which only
  // ever shows the filtered subset.
  selectEl._picked = new Set(opts.selectedCodes || []);
  _mountProductSearch(selectEl);
  _renderProductOptions(selectEl);

  if (opts.selectFirst && !selectEl._picked.size && rows.length) {
    selectEl._picked.add(rows[0].code);
    _renderProductOptions(selectEl);
  }
}

function _mountProductSearch(selectEl) {
  if (selectEl._searchEl) return;
  const box = document.createElement("input");
  box.type = "search";
  box.className = "product-search";
  box.placeholder = "Search code or name…";
  box.autocomplete = "off";
  box.addEventListener("input", () => _renderProductOptions(selectEl));
  selectEl.parentNode.insertBefore(box, selectEl);
  selectEl._searchEl = box;
  // The DOM is the source of truth for a click; mirror it back into the set so
  // a selection made before typing survives the next filter.
  selectEl.addEventListener("change", () => {
    const shown = new Set(Array.from(selectEl.options).map(o => o.value));
    const picked = new Set(Array.from(selectEl.selectedOptions).map(o => o.value));
    // Only reconcile what is on screen; a selected row hidden by the filter
    // keeps its state.
    selectEl._picked.forEach(c => { if (shown.has(c) && !picked.has(c)) selectEl._picked.delete(c); });
    picked.forEach(c => selectEl._picked.add(c));
    _renderProductCount(selectEl);
  });
}

function _renderProductOptions(selectEl) {
  const rows = selectEl._rows || [];
  const q = (selectEl._searchEl ? selectEl._searchEl.value : "").trim().toLowerCase();
  const match = r => !q || r.code.toLowerCase().includes(q) ||
    (r.name || "").toLowerCase().includes(q);
  selectEl.innerHTML = "";
  rows.forEach(r => {
    if (!match(r) && !selectEl._picked.has(r.code)) return;
    const option = document.createElement("option");
    option.value = r.code;
    option.textContent = selectEl._labelFn(r);
    option.selected = selectEl._picked.has(r.code);
    selectEl.appendChild(option);
  });
  _renderProductCount(selectEl);
}

// "3 of 24" under the box, so a filter hiding most of the list never reads as
// an empty product table.
function _renderProductCount(selectEl) {
  let el = selectEl._countEl;
  if (!el) {
    el = document.createElement("div");
    el.className = "product-count";
    selectEl.parentNode.insertBefore(el, selectEl.nextSibling);
    selectEl._countEl = el;
  }
  const total = (selectEl._rows || []).length;
  const shown = selectEl.options.length;
  const picked = selectEl._picked ? selectEl._picked.size : 0;
  el.textContent = (shown === total ? `${total}` : `${shown} of ${total}`) +
    (picked ? ` · ${picked} selected` : "");
}

// Reads the picked set, not selectedOptions — the DOM only holds whatever the
// search box is currently showing.
function currentSelection(id) {
  return selectedCodes(document.getElementById(id));
}

// Add or remove `codes` from a picker's selection and re-render.
function setProductSelection(el, codes, on) {
  if (!el || !el._picked) return;
  codes.forEach(c => (on ? el._picked.add(c) : el._picked.delete(c)));
  _renderProductOptions(el);
}

// Same, for callers holding the element rather than its id.
function selectedCodes(el) {
  if (!el) return [];
  if (el._picked) return Array.from(el._picked);
  return Array.from(el.selectedOptions).map(o => o.value);
}

// Fetch all three product lists and populate every product/stock <select> on
// the page. Called once from _boot(), and again after every add/remove so
// derived selects (the two-list intersections) stay correct. Each select's
// selection is preserved across a refresh.
async function initProductSelects() {
  // The US ADR chain is admin-only; user mode is Taiwan instruments only, so
  // don't even ask for the list.
  const [warrants, twOpts, usOpts] = await Promise.all([
    fetchProductList("/list_warrant_stocks"),
    fetchProductList("/list_tw_option_products"),
    can("usoptions") ? fetchProductList("/list_us_option_products") : Promise.resolve([]),
  ]);
  // Exposed for the add/remove UI to reuse without re-fetching.
  window._productLists = { warrants, twOpts, usOpts };

  const codesOf = rows => new Set(rows.map(r => r.code));
  const twCodes = codesOf(twOpts), usCodes = codesOf(usOpts);
  const warrantXTw = warrants.filter(r => twCodes.has(r.code));
  const warrantXUs = warrants.filter(r => usCodes.has(r.code));
  const twXUs = twOpts.filter(r => usCodes.has(r.code));

  const sel = (id, fallback) => {
    const cur = currentSelection(id);
    return cur.length ? cur : fallback;
  };

  populateProductSelect(document.getElementById("stockSelect"), warrants,
    { selectedCodes: sel("stockSelect", []) });
  populateProductSelect(document.getElementById("ivStockSelect"), warrants,
    { selectedCodes: sel("ivStockSelect", []) });
  populateProductSelect(document.getElementById("optionStockSelect"), twOpts,
    { selectedCodes: sel("optionStockSelect", ["TXO"]) });
  populateProductSelect(document.getElementById("optionUsSelect"), usOpts,
    { labelFn: r => r.name || r.code, selectedCodes: sel("optionUsSelect", ["2303"]) });
  populateProductSelect(document.getElementById("ivOptProduct"), twOpts,
    { selectedCodes: sel("ivOptProduct", []), selectFirst: true });
  populateProductSelect(document.getElementById("arbStockSelect"), warrantXTw,
    { selectedCodes: sel("arbStockSelect", ["2330"]) });
  // The static-arb LP scans the warrant∩TW-option overlap.
  populateProductSelect(document.getElementById("expStockSelect"), warrantXTw,
    { selectedCodes: sel("expStockSelect", ["2330"]) });
  populateProductSelect(document.getElementById("usStockSelect"), warrantXUs,
    { selectedCodes: sel("usStockSelect", ["2303"]) });
  populateProductSelect(document.getElementById("twusStockSelect"), twXUs,
    { selectedCodes: sel("twusStockSelect", ["2303"]) });
}

// ── Bound-filter panel ───────────────────────────────────────────────
// One dropdown chooses which filter the Min/Max inputs edit. Every filter stays
// applied with whatever it holds, so the summary lists ALL of them — a bound set
// under a different dropdown entry would otherwise shape the results with
// nothing on screen to explain it. A blank bound is omitted from the request,
// which leaves the route's own default in force.
//
// A spec entry is { key, label, min?, max? }, where each side is
// { field, value, attrs? } — `field` is the request key, `value` the default.
// A filter with only one meaningful side declares only that side.
const _BF = {};

function mountBoundFilters(hostId, spec) {
  const host = document.getElementById(hostId);
  if (!host) return null;
  const state = {};
  spec.forEach(f => {
    state[f.key] = { min: f.min ? String(f.min.value ?? "") : "",
                     max: f.max ? String(f.max.value ?? "") : "" };
  });
  _BF[hostId] = { spec, state, kind: spec[0].key };
  host.classList.add("filters", "bound-filters");
  host.innerHTML = `
    <label>Filter
      <select onchange="bfPick('${hostId}', this.value)">
        ${spec.map(f => `<option value="${f.key}">${f.label}</option>`).join("")}
      </select>
    </label>
    <label class="bf-side" data-side="min">Min
      <input type="number" step="any" placeholder="none"
             oninput="bfInput('${hostId}', 'min', this.value)" /></label>
    <label class="bf-side" data-side="max">Max
      <input type="number" step="any" placeholder="none"
             oninput="bfInput('${hostId}', 'max', this.value)" /></label>
    <div class="action-group compact">
      <button class="sm" onclick="bfReset('${hostId}')" title="Back to the default bounds">Reset</button>
    </div>
    <div class="bf-summary"></div>`;
  bfPick(hostId, spec[0].key);
  return _BF[hostId];
}

function _bfEntry(hostId) {
  const c = _BF[hostId];
  return c ? c.spec.find(f => f.key === c.kind) : null;
}

function bfPick(hostId, kind) {
  const c = _BF[hostId];
  if (!c) return;
  c.kind = kind;
  const host = document.getElementById(hostId);
  const entry = _bfEntry(hostId);
  host.querySelectorAll(".bf-side").forEach(el => {
    const side = el.dataset.side;
    // A one-sided filter hides the slot it does not have, rather than offering
    // a box whose value would be silently dropped.
    el.style.display = entry[side] ? "" : "none";
    const input = el.querySelector("input");
    input.value = c.state[kind][side];
    if (entry[side] && entry[side].attrs) {
      for (const k in entry[side].attrs) input.setAttribute(k, entry[side].attrs[k]);
    } else {
      input.removeAttribute("min"); input.removeAttribute("max");
    }
  });
  bfRender(hostId);
}

function bfInput(hostId, side, value) {
  const c = _BF[hostId];
  if (!c) return;
  c.state[c.kind][side] = value;
  bfRender(hostId);
}

function bfReset(hostId) {
  const c = _BF[hostId];
  if (!c) return;
  c.spec.forEach(f => {
    c.state[f.key] = { min: f.min ? String(f.min.value ?? "") : "",
                       max: f.max ? String(f.max.value ?? "") : "" };
  });
  bfPick(hostId, c.kind);
}

function bfRender(hostId) {
  const c = _BF[hostId];
  const el = document.getElementById(hostId).querySelector(".bf-summary");
  if (!c || !el) return;
  const parts = [];
  for (const f of c.spec) {
    const { min, max } = c.state[f.key];
    if (min === "" && max === "") continue;
    const short = f.short || f.label;
    if (f.min && f.max) parts.push(`${short} ${min === "" ? "−∞" : min}–${max === "" ? "∞" : max}`);
    else if (f.min) parts.push(`${short} ≥ ${min}`);
    else parts.push(`${short} ≤ ${max}`);
  }
  el.textContent = parts.length ? parts.join("  ·  ") : "No filters";
}

// Request fields for every non-blank bound. Blank means "not sent", so the
// route's own default applies.
function bfPayload(hostId) {
  const c = _BF[hostId];
  const out = {};
  if (!c) return out;
  for (const f of c.spec) {
    for (const side of ["min", "max"]) {
      const slot = f[side];
      const v = c.state[f.key][side];
      if (slot && v !== "") out[slot.field] = v;
    }
  }
  return out;
}

// ── Boot ─────────────────────────────────────────────────────────────
// Auth gate + first render. Lives here, not in portfolio.js, because user mode
// never loads portfolio.js and would otherwise boot without a session check.
// Deferred to DOMContentLoaded so the admin-only scripts loaded after this file
// have defined loadPortfolioOnce/loadHomeOnce by the time restoreView runs.

async function _boot() {
  if (!LOCAL_MODE && SUPABASE_URL && !_sb) {
    // Auth is configured but supabase-js never loaded (CDN blocked).
    document.body.innerHTML =
      '<div style="max-width:420px;margin:15vh auto;text-align:center;font-family:inherit;color:#d8dee4">' +
      '<h2 style="font-size:18px;margin-bottom:10px">Could not load the sign-in library</h2>' +
      '<p style="color:#7b8794;font-size:13px">The script from cdn.jsdelivr.net was blocked. Disable ad-blockers for this site and reload.</p></div>';
    return;
  }
  if (_sb) {
    const { data } = await _sb.auth.getSession();
    if (!data.session) { location.replace("/login"); return; }
    _sb.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_OUT" || !session) location.replace("/login");
    });
  }
  await initProductSelects();
  // Mount each scanner's filter panel. After initProductSelects so the panels
  // sit in a fully built filter row rather than shifting it as they appear.
  if (typeof mountScannerFilters === "function") mountScannerFilters();
  // Portfolio is loaded lazily on first Portfolio-tab click (loadPortfolioOnce),
  // not here — the landing tab doesn't use it. Saves /get_portfolio +
  // /adr_premium_scenario calls on every reload.
  restoreView();   // put the user back on the tab/market they had before reload
}

document.addEventListener("DOMContentLoaded", _boot);
