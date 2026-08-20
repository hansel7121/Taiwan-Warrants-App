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
function populateProductSelect(selectEl, rows, opts) {
  if (!selectEl) return;
  opts = opts || {};
  const labelFn = opts.labelFn || (r => r.name ? `${r.code} ${r.name}` : r.code);
  const selectedCodes = opts.selectedCodes || [];
  selectEl.innerHTML = "";
  rows.forEach(r => {
    const option = document.createElement("option");
    option.value = r.code;
    option.textContent = labelFn(r);
    if (selectedCodes.includes(r.code)) option.selected = true;
    selectEl.appendChild(option);
  });
  if (opts.selectFirst && selectEl.options.length && !selectEl.value) {
    selectEl.options[0].selected = true;
  }
}

function currentSelection(id) {
  const el = document.getElementById(id);
  return el ? Array.from(el.selectedOptions).map(o => o.value) : [];
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
  // Straddle Vol Arb needs BOTH a warrant and a TW option on the underlying —
  // same warrant∩TW-option overlap as Direct Match, so it stays correct as
  // products are added (no hardcoded code list).
  populateProductSelect(document.getElementById("straddleStockSelect"), warrantXTw,
    { selectedCodes: sel("straddleStockSelect", ["2330"]) });
  // Both static-arb LP tabs need the same warrant∩TW-option overlap.
  populateProductSelect(document.getElementById("expStockSelect"), warrantXTw,
    { selectedCodes: sel("expStockSelect", ["2330"]) });
  populateProductSelect(document.getElementById("swStockSelect"), warrantXTw,
    { selectedCodes: sel("swStockSelect", ["2330"]) });
  populateProductSelect(document.getElementById("usStockSelect"), warrantXUs,
    { selectedCodes: sel("usStockSelect", ["2303"]) });
  populateProductSelect(document.getElementById("twusStockSelect"), twXUs,
    { selectedCodes: sel("twusStockSelect", ["2303"]) });
}

// ── Market session clock (NYSE / TWSE / TAIFEX options) ──────────────
// NYSE runs in US Eastern; TWSE and TAIFEX in Taipei. Sessions are wall-
// clock ranges in each exchange's own timezone (minutes past midnight).
// Holidays are not modeled — weekday hours only.
function _tzParts(tz) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour12: false,
    weekday: "short", hour: "2-digit", minute: "2-digit" });
  const o = {}; f.formatToParts(new Date()).forEach(p => o[p.type] = p.value);
  const h = parseInt(o.hour, 10) % 24, m = parseInt(o.minute, 10);
  return { wd: o.weekday, mins: h * 60 + m, str: String(h).padStart(2, "0") + ":" + o.minute };
}
function _mcSet(id, txt, cls) {
  const e = document.getElementById(id); if (!e) return;
  e.textContent = txt; e.className = "mc-badge " + cls;
}
function updateMarketClock() {
  // The widget is admin-only, so the elements are absent in user mode.
  const nyEl = document.getElementById("mc-ny-time");
  if (!nyEl) return;
  const et = _tzParts("America/New_York"), tp = _tzParts("Asia/Taipei"),
        pt = _tzParts("America/Los_Angeles");
  nyEl.textContent = et.str;
  document.getElementById("mc-pt-time").textContent = pt.str;
  document.getElementById("mc-tw-time").textContent = tp.str;
  const nyWknd = et.wd === "Sat" || et.wd === "Sun", nt = et.mins;
  // NYSE: pre 04:00–09:30, regular 09:30–16:00, after 16:00–20:00
  if (!nyWknd && nt >= 570 && nt < 960) _mcSet("mc-ny-st", "Open", "mc-open");
  else if (!nyWknd && nt >= 960 && nt < 1200) _mcSet("mc-ny-st", "After-hrs", "mc-after");
  else if (!nyWknd && nt >= 240 && nt < 570) _mcSet("mc-ny-st", "Pre-mkt", "mc-after");
  else _mcSet("mc-ny-st", "Closed", "mc-closed");
  // TWSE stocks: 09:00–13:30
  const twWknd = tp.wd === "Sat" || tp.wd === "Sun", tt = tp.mins;
  if (!twWknd && tt >= 540 && tt < 810) _mcSet("mc-tw-st", "Open", "mc-open");
  else _mcSet("mc-tw-st", "Closed", "mc-closed");
  // TAIFEX options: regular 08:45–13:45; after-hours 15:00–05:00 next day.
  // Evening leg (15:00–24:00) runs Mon–Fri; morning leg (00:00–05:00) is
  // the continuation of the prior weekday session, so it's live Tue–Sat.
  const weekday = !twWknd;
  if (weekday && tt >= 525 && tt < 825) _mcSet("mc-tx-st", "Regular", "mc-open");
  else if (weekday && tt >= 900) _mcSet("mc-tx-st", "After-hrs", "mc-after");
  else if (tt < 300 && tp.wd !== "Sun" && tp.wd !== "Mon") _mcSet("mc-tx-st", "After-hrs", "mc-after");
  else _mcSet("mc-tx-st", "Closed", "mc-closed");
}
// ── Hover tooltips: trading hours per exchange, in the USER's local tz ──
// Session ranges are wall-clock minutes past midnight in each exchange's own
// timezone; end < start (e.g. TAIFEX night) is expressed as end + 1440.
const MC_SESSIONS = {
  "mc-row-ny": { name: "NYSE", tz: "America/New_York",
    rows: [["Pre-market", 240, 570], ["Regular", 570, 960], ["After-hours", 960, 1200]] },
  "mc-row-tw": { name: "TWSE", tz: "Asia/Taipei",
    rows: [["Regular", 540, 810]] },
  "mc-row-tx": { name: "TAIFEX opt", tz: "Asia/Taipei",
    rows: [["Regular", 525, 825], ["After-hours", 900, 1740]] },
};
// Minutes a timezone is ahead of UTC at instant `at` (DST-aware).
function _tzOffset(tz, at) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const p = {}; f.formatToParts(at).forEach(x => p[x.type] = x.value);
  const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
  return Math.round((asUTC - at.getTime()) / 60000);
}
const _hm = (min) => { min = ((min % 1440) + 1440) % 1440;
  return String(Math.floor(min / 60)).padStart(2, "0") + ":" + String(min % 60).padStart(2, "0"); };
// Convert an exchange-tz wall-clock range to the viewer's local wall-clock,
// tagging any endpoint that lands on a different local day (+1d / -1d).
function _mcRange(tz, s, e) {
  const now = new Date();
  const delta = (-now.getTimezoneOffset()) - _tzOffset(tz, now);
  const sa = s + delta, ea = e + delta, base = Math.floor(sa / 1440);
  const mark = (v) => { const d = Math.floor(v / 1440) - base;
    return d === 0 ? "" : (d > 0 ? ` (+${d}d)` : ` (${d}d)`); };
  return _hm(sa) + mark(sa) + "–" + _hm(ea) + mark(ea);
}
function mcBuildTips() {
  for (const id in MC_SESSIONS) {
    const row = document.getElementById(id); if (!row) continue;
    let tip = row.querySelector(".mc-tip");
    if (!tip) { tip = document.createElement("div"); tip.className = "mc-tip"; row.appendChild(tip); }
    const cfg = MC_SESSIONS[id];
    let html = '<div class="mc-tip-h">' + cfg.name + " · your local time</div>";
    for (const [label, s, e] of cfg.rows)
      html += '<div class="mc-tip-r"><span class="mc-tip-k">' + label +
        '</span><span class="mc-tip-v">' + _mcRange(cfg.tz, s, e) + "</span></div>";
    tip.innerHTML = html;
  }
}

if (can("clock")) {
  updateMarketClock(); mcBuildTips();
  setInterval(() => { updateMarketClock(); mcBuildTips(); }, 15000);
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
    document.getElementById("logoutBtn").style.display = "block";
    _sb.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_OUT" || !session) location.replace("/login");
    });
  }
  await initProductSelects();
  // Portfolio is loaded lazily on first Portfolio-tab click (loadPortfolioOnce),
  // not here — the landing tab doesn't use it. Saves /get_portfolio +
  // /adr_premium_scenario calls on every reload.
  restoreView();   // put the user back on the tab/market they had before reload
}

document.addEventListener("DOMContentLoaded", _boot);
