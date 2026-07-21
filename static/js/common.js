// Shared: Supabase auth, fetch wrapper, tab nav + view persistence, table helpers.

const DEFAULT_STOCKS = [
  "2330","2317","2454","2382","3231","6669","2376","3017","3324",
  "2308","3711","3034","2379","3661","3443","2603","3008","2881",
  "2882","3037","2303","2886",
];

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
      '<div style="max-width:420px;margin:15vh auto;text-align:center;font-family:inherit;color:#e2e8f0">' +
      '<h2 style="font-size:18px;margin-bottom:10px">Your account is not approved yet</h2>' +
      '<p style="color:#8b90a0;font-size:13px">Ask the administrator to add your email to the allow-list.</p>' +
      '<p style="margin-top:16px"><a href="#" onclick="_logout();return false" style="color:#4f8ef7">Sign out</a></p></div>';
    throw new Error("not_allowed");
  }
  return res;
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
// the button, even on error.

async function refreshNow(kind, statusEl, btn, onDone, tableEl) {
  if (!statusEl || !btn) return;
  const origLabel = btn.textContent;
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  btn.textContent = "Syncing…";
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
  const tab = _readView("ws_activeTab");
  if (tab && tab !== "options") {
    const btn = document.querySelector(`.tab-bar button[onclick*="switchTab('${tab}'"]`);
    if (btn) { switchTab(tab, btn); if (tab === "portfolio") loadPortfolioOnce(); }
  }
  if (_readView("ws_optMarket") === "us") {
    const b = document.getElementById("optmkt-btn-us");
    if (b) setOptMarket("us", b);
  }
}

const HIDDEN_COLS = ["warrant_iv", "opt_iv", "iv_diff",
  // TW/US raw depth fields — surfaced in the popup panel, not the table.
  "tw_depth_contracts", "tw_fillable", "us_volume", "us_oi"];
// The US Option Match "us_stock_code" value is actually the TW stock code.

const COL_LABELS = { us_stock_code: "tw_stock_code",
                     warrant_depth_lots: "depth (張)", fillable: "fillable?" };

const visCols = (row) => Object.keys(row).filter(c => !HIDDEN_COLS.includes(c));

const colLabel = (c) => COL_LABELS[c] || c;
