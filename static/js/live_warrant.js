// Live warrant sub-tab: broker credential entry (issue #42), the shared
// Watchlist editor (issue #44), the connection-status panel (issue #45) and the
// live price stream (issue #46).
//
// Nothing here stores or echoes a secret: the password inputs are read once at
// submit, posted straight to /save_broker_credential, and cleared. The saved
// list is metadata only (the backend never returns a credential field), so
// there is nothing secret to render.

// Which inputs belong to which broker. KGI takes no cert (its cert setup is a
// manual one-time CLI step outside the app) and Fubon takes no Capacity Tier
// (fixed 200/5 for every account) — so each broker's form is a strict subset,
// toggled by the .bc-kgi / .bc-fubon classes in index.html.
const BC_FIELDS = {
  kgi:   { person_id: "bcPersonId", person_pwd: "bcPersonPwd" },
  fubon: { person_id: "bcPersonId", password: "bcPassword", cert_pass: "bcCertPass" },
};

function switchScannerSub(sub, btn) {
  document.querySelectorAll(".scsub-content").forEach(el => el.style.display = "none");
  document.querySelectorAll("#tab-scanner .pfsub-btn").forEach(el => el.classList.remove("active"));
  document.getElementById("scsub-" + sub).style.display = "block";
  if (btn) btn.classList.add("active");
  _saveView("ws_scannerSub", sub);
  if (sub === "live") {
    loadBrokerCredentials();
    loadWatchlistCodes();
    loadConnectionStatus();
    startLivePriceStream();
  }
}

// Show only the selected broker's fields.
function onBrokerChange() {
  const broker = document.getElementById("bcBroker").value;
  document.querySelectorAll(".bc-kgi").forEach(el =>
    el.style.display = broker === "kgi" ? "" : "none");
  document.querySelectorAll(".bc-fubon").forEach(el =>
    el.style.display = broker === "fubon" ? "" : "none");
  _bcStatus("");
}

function _bcStatus(msg, isError) {
  const el = document.getElementById("bcStatus");
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function _bcVal(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : "";
}

async function saveBrokerCredential() {
  const broker = document.getElementById("bcBroker").value;
  const form = new FormData();
  form.append("broker", broker);

  for (const [field, inputId] of Object.entries(BC_FIELDS[broker])) {
    const val = _bcVal(inputId);
    if (!val) { _bcStatus(`${field.replace(/_/g, " ")} is required`, true); return; }
    form.append(field, val);
  }

  if (broker === "fubon") {
    const file = document.getElementById("bcCert").files[0];
    // Optional: a re-save of the text fields must not force re-uploading a
    // cert that is already stored.
    if (file) {
      const name = file.name.toLowerCase();
      if (!name.endsWith(".pfx") && !name.endsWith(".p12")) {
        _bcStatus("Certificate must be a .p12 or .pfx file", true); return;
      }
      form.append("cert", file);
    }
  } else {
    // Blank tier inputs are simply not sent, so the server applies its default.
    const symbols = _bcVal("bcSymbols"), connections = _bcVal("bcConnections");
    if (symbols) form.append("symbols_per_connection", symbols);
    if (connections) form.append("connections", connections);
  }

  const btn = document.getElementById("bcSaveBtn");
  btn.disabled = true;
  _bcStatus("Saving…");
  try {
    const res = await api("/save_broker_credential", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) { _bcStatus(data.error || "Save failed", true); return; }
    _bcStatus(`Saved ${broker.toUpperCase()} credential.`);
    _bcClearInputs();
    await loadBrokerCredentials();
  } catch (e) {
    _bcStatus("Save failed: " + (e && e.message ? e.message : e), true);
  } finally {
    btn.disabled = false;
  }
}

// Clear every secret input as soon as the post completes, so a password is not
// left sitting in the DOM for the rest of the session.
function _bcClearInputs() {
  ["bcPersonId", "bcPersonPwd", "bcPassword", "bcCertPass",
   "bcSymbols", "bcConnections", "bcCert"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
}

async function loadBrokerCredentials() {
  const container = document.getElementById("bcSavedContainer");
  if (!container) return;
  let rows = [];
  try {
    const res = await api("/list_broker_credentials");
    rows = await res.json();
  } catch (e) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">Could not load saved credentials.</div>';
    return;
  }
  if (!rows || !rows.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No broker credential saved yet.</div>';
    return;
  }
  let html = '<table style="border-collapse:collapse;font-size:13px">'
    + '<tr><th style="text-align:left;padding:4px 12px 4px 0">Broker</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Capacity tier</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Cert</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Updated</th>'
    + '<th></th></tr>';
  rows.forEach(r => {
    const tier = `${r.symbols_per_connection} × ${r.connections}`;
    const cert = r.has_cert ? "uploaded" : "—";
    const updated = r.updated_at ? new Date(r.updated_at).toLocaleString() : "—";
    html += `<tr><td style="padding:4px 12px 4px 0">${r.broker.toUpperCase()}</td>`
      + `<td style="padding:4px 12px 4px 0">${tier}</td>`
      + `<td style="padding:4px 12px 4px 0">${cert}</td>`
      + `<td style="padding:4px 12px 4px 0">${updated}</td>`
      + `<td><button class="sm" onclick="removeBrokerCredential('${r.broker}')">Remove</button></td></tr>`;
  });
  container.innerHTML = html + "</table>";
}

async function removeBrokerCredential(broker) {
  if (!confirm(`Remove your ${broker.toUpperCase()} credential?`)) return;
  try {
    const res = await api("/remove_broker_credential", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ broker }),
    });
    const data = await res.json();
    if (!res.ok) { _bcStatus(data.error || "Remove failed", true); return; }
    _bcStatus(`Removed ${broker.toUpperCase()} credential.`);
    await loadBrokerCredentials();
  } catch (e) {
    _bcStatus("Remove failed: " + (e && e.message ? e.message : e), true);
  }
}

// ── Shared Watchlist ─────────────────────────────────────────────────────────
//
// One list for the whole app, not per-user: every watched code occupies a slot
// in the shared connection pool, so an add can be rejected because someone
// else filled the pool. The backend words that rejection (it knows the live
// N/M counts), and we surface its message verbatim.

function _wlStatus(msg, isError) {
  const el = document.getElementById("wlStatus");
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "var(--muted)";
}

// "031100" -> ["031100"]; "031100, 031101" -> ["031100", "031101"]. Blank
// pieces from a trailing comma or a double comma are dropped.
function _wlParseCodes(raw) {
  return raw.split(",").map(c => c.trim()).filter(c => c);
}

async function addWatchlistCodes() {
  const input = document.getElementById("wlCodes");
  const codes = _wlParseCodes(input ? input.value : "");
  if (!codes.length) { _wlStatus("Enter a warrant or TXO option code first.", true); return; }

  const btn = document.getElementById("wlAddBtn");
  btn.disabled = true;
  _wlStatus("Adding…");
  try {
    const res = await api("/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes }),
    });
    const data = await res.json();
    // A full pool comes back as a 400 whose message already names the exact
    // N/M counts — show it as-is rather than paraphrasing.
    if (!res.ok || !data.ok) { _wlStatus(data.error || "Add failed", true); return; }
    input.value = "";
    const added = data.codes || [];
    _wlStatus(added.length
      ? `Added ${added.join(", ")}.`
      : "Already on the watchlist.");
    await loadWatchlistCodes();
  } catch (e) {
    _wlStatus("Add failed: " + (e && e.message ? e.message : e), true);
  } finally {
    btn.disabled = false;
  }
}

async function removeWatchlistCode(code) {
  try {
    const res = await api("/watchlist/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes: [code] }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) { _wlStatus(data.error || "Remove failed", true); return; }
    // Refetch rather than just dropping the row: the freed capacity has to be
    // real before the next add, and the list is shared so it may have moved.
    _wlStatus(`Removed ${code}.`);
    await loadWatchlistCodes();
  } catch (e) {
    _wlStatus("Remove failed: " + (e && e.message ? e.message : e), true);
  }
}

async function loadWatchlistCodes() {
  const container = document.getElementById("wlCodesContainer");
  if (!container) return;
  let codes = [];
  try {
    const res = await api("/watchlist/list");
    const data = await res.json();
    codes = (data && data.codes) || [];
  } catch (e) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">Could not load the watchlist.</div>';
    return;
  }
  if (!codes.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No code watched yet.</div>';
    return;
  }
  let html = '<table style="border-collapse:collapse;font-size:13px">'
    + '<tr><th style="text-align:left;padding:4px 12px 4px 0">Code</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Instrument</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Price</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Updated</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Status</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Depth</th>'
    + '<th></th></tr>';
  codes.forEach(code => {
    const safe = _wlSafe(code);
    // Per-code cell ids so the stream can write one row without re-rendering
    // the table under whatever the user is mid-click on.
    html += `<tr><td style="padding:4px 12px 4px 0">${safe}</td>`
      + `<td style="padding:4px 12px 4px 0;color:var(--muted)">${_wlInstrumentLabel(code)}</td>`
      + `<td id="wl-price-${safe}" style="padding:4px 12px 4px 0">—</td>`
      + `<td id="wl-ts-${safe}" style="padding:4px 12px 4px 0">—</td>`
      + `<td id="wl-status-${safe}" style="padding:4px 12px 4px 0;color:var(--muted)">—</td>`
      + `<td><button class="sm" id="wl-depth-toggle-${safe}" onclick="_wlToggleDepth('${safe}')">▸ depth</button></td>`
      + `<td><button class="sm" onclick="removeWatchlistCode('${safe}')">Remove</button></td></tr>`
      + `<tr id="wl-depth-row-${safe}" style="display:none"><td colspan="7" id="wl-depth-${safe}"></td></tr>`;
  });
  container.innerHTML = html + "</table>";
}

// Codes are rendered into ids and an inline onclick, so strip anything that is
// not alphanumeric before either use.
function _wlSafe(code) {
  return String(code).replace(/[^0-9A-Za-z]/g, "");
}

// Mirrors kgi_client.py::_is_option_code (#55): TWSE codes are all-digits,
// TAIFEX option roots (e.g. TXO) start with a letter. Display-only — the
// stream payload's own "instrument" field is the source of truth once a code
// has ticked; this just labels a row before its first tick arrives.
function _wlInstrumentLabel(code) {
  return /^\d/.test(code) ? "Warrant" : "TW Option";
}

// ── Connection status ────────────────────────────────────────────────────────
//
// One row per Broker Account in the shared pool — every user's, not just the
// caller's, because any account may be carrying a code you watch. Rows are
// labelled "(you)" rather than by owner: the backend has no user_id -> email
// mapping to name anyone with.
//
// Fetched on tab activation like everything else here; the worker writes its
// status on every transition, so reopening the tab is the refresh.

const CS_COLOURS = {
  connected:    "var(--success)",
  reconnecting: "var(--accent)",
  disconnected: "var(--danger)",
  stopped:      "var(--muted)",
};

async function loadConnectionStatus() {
  const container = document.getElementById("csContainer");
  if (!container) return;
  let rows = [];
  try {
    const res = await api("/broker/status");
    rows = await res.json();
  } catch (e) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">Could not load connection status.</div>';
    return;
  }
  if (!rows || !rows.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No broker account to connect yet.</div>';
    return;
  }
  let html = '<table style="border-collapse:collapse;font-size:13px">'
    + '<tr><th style="text-align:left;padding:4px 12px 4px 0">Account</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Status</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Subscribed</th><th></th></tr>';
  rows.forEach(r => {
    const account = r.broker.toUpperCase() + (r.is_you ? " (you)" : "");
    const status = r.status || "not yet reported";
    const colour = CS_COLOURS[r.status] || "var(--muted)";
    // No status row at all means no worker has ever acted on this account, so
    // printing its share of the watchlist would claim a subscription that has
    // never existed.
    const subscribed = r.status ? `${r.subscribed}/${r.capacity}` : `—/${r.capacity}`;
    // Only the caller's own account can be told to connect/disconnect — this
    // panel lists every account in the shared pool, not just the caller's.
    let action = "";
    if (r.is_you) {
      const live = r.status === "connected" || r.status === "reconnecting";
      action = live
        ? `<button class="sm" onclick="disconnectBroker('${r.broker}')">Disconnect</button>`
        : `<button class="sm" onclick="connectBroker('${r.broker}')">Connect</button>`;
    }
    html += `<tr><td style="padding:4px 12px 4px 0">${account}</td>`
      + `<td style="padding:4px 12px 4px 0;color:${colour}">${status}</td>`
      + `<td style="padding:4px 12px 4px 0">${subscribed}</td>`
      + `<td>${action}</td></tr>`;
  });
  container.innerHTML = html + "</table>";
}

// Records connect/disconnect intent (broker_desired_state); the worker picks
// it up on its own poll (~20s), so the panel is re-fetched after a short delay
// rather than expecting an immediate status flip.
async function connectBroker(broker) {
  await _setBrokerDesiredState("/broker/connect", broker);
}

async function disconnectBroker(broker) {
  await _setBrokerDesiredState("/broker/disconnect", broker);
}

async function _setBrokerDesiredState(path, broker) {
  try {
    const res = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ broker }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) { _bcStatus(data.error || "Request failed", true); return; }
  } catch (e) {
    _bcStatus("Request failed: " + (e && e.message ? e.message : e), true);
    return;
  }
  await loadConnectionStatus();
  setTimeout(loadConnectionStatus, 5000);
}

// ── Live prices ─────────────────────────────────────────────────────────────
//
// One SSE connection for the whole session, kept open across sub-tab switches:
// the server pushes a frame a second, so reopening the tab would otherwise pile
// up parallel streams. A code with no tick yet is simply absent from the frame
// (ADR-0005), and a code whose worker dropped keeps its last price marked
// "stale" rather than being blanked.

let _lpSource = null;

async function startLivePriceStream() {
  if (_lpSource && _lpSource.readyState !== EventSource.CLOSED) return;

  // SSE_STREAM_BASE is empty in local dev (same-origin, mounted into app.py);
  // in production it's the standalone SSE service's own origin (issue #58
  // item 1), so this request goes cross-origin — see sse.py's CORS header.
  let url = window.SSE_STREAM_BASE + "/live_prices/stream";
  // EventSource cannot set an Authorization header, so the bearer token rides
  // in the query string. Local mode has no Supabase client and no auth at all.
  if (!LOCAL_MODE && _sb) {
    const { data } = await _sb.auth.getSession();
    const token = data.session && data.session.access_token;
    if (!token) return;
    url += "?token=" + encodeURIComponent(token);
  }

  _lpSource = new EventSource(url);
  _lpSource.onmessage = ev => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch (e) { return; }
    Object.keys(payload || {}).forEach(code => _lpApplyRow(code, payload[code]));
  };
  // EventSource reconnects by itself; nothing to retry here, and a dropped
  // stream must not pop an alert over the rest of the tab.
  _lpSource.onerror = () => console.warn("live price stream interrupted");
}

function _lpApplyRow(code, row) {
  const safe = _wlSafe(code);
  const priceEl = document.getElementById("wl-price-" + safe);
  // Not on the currently rendered watchlist (just removed, or another tab is
  // showing) — skip rather than treating it as an error.
  if (!priceEl || !row) return;
  priceEl.textContent = row.price != null ? row.price : "—";

  const tsEl = document.getElementById("wl-ts-" + safe);
  if (tsEl) tsEl.textContent = row.ts ? new Date(row.ts).toLocaleTimeString() : "—";

  const statusEl = document.getElementById("wl-status-" + safe);
  if (statusEl) {
    statusEl.textContent = row.is_live ? "live" : "stale";
    statusEl.style.color = row.is_live ? CS_COLOURS.connected : CS_COLOURS.stopped;
  }

  _wlObserveDepth(code, row.depth);
  _tlObserve(code, row);
}

// ── Depth ladder (issue #51) ────────────────────────────────────────────────
// A 5-level bid/ask ladder per watched code, collapsed by default (screen
// space) and expanded on click. Kept up to date from the same SSE stream as
// price even while collapsed, so opening it shows the current book at once
// rather than waiting for the next frame.

const _wlDepthOpen = {};
const _wlLastDepth = {};

function _wlToggleDepth(safe) {
  _wlDepthOpen[safe] = !_wlDepthOpen[safe];
  const row = document.getElementById("wl-depth-row-" + safe);
  const btn = document.getElementById("wl-depth-toggle-" + safe);
  if (row) row.style.display = _wlDepthOpen[safe] ? "" : "none";
  if (btn) btn.textContent = (_wlDepthOpen[safe] ? "▾" : "▸") + " depth";
  if (_wlDepthOpen[safe]) _wlRenderDepth(safe, _wlLastDepth[safe]);
}

function _wlObserveDepth(code, depth) {
  const safe = _wlSafe(code);
  _wlLastDepth[safe] = depth;
  if (_wlDepthOpen[safe]) _wlRenderDepth(safe, depth);
}

function _wlRenderDepth(safe, depth) {
  const cell = document.getElementById("wl-depth-" + safe);
  if (!cell) return;
  if (!depth) {
    cell.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:4px 0">No depth yet.</div>';
    return;
  }
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `<tr>`
      + `<td style="padding:2px 8px;text-align:right">${depth.bid_volumes[i]}</td>`
      + `<td style="padding:2px 8px;text-align:right;color:var(--success)">${depth.bid_prices[i]}</td>`
      + `<td style="padding:2px 8px;text-align:right;color:var(--danger)">${depth.ask_prices[i]}</td>`
      + `<td style="padding:2px 8px;text-align:right">${depth.ask_volumes[i]}</td>`
      + `</tr>`;
  }
  cell.innerHTML = '<table style="border-collapse:collapse;font-size:12px;margin:4px 0">'
    + '<tr><th style="padding:2px 8px">Bid vol</th><th style="padding:2px 8px">Bid</th>'
    + '<th style="padding:2px 8px">Ask</th><th style="padding:2px 8px">Ask vol</th></tr>'
    + rows + '</table>';
}

// ── Trade log (issue #54) ────────────────────────────────────────────────
// Client-side-only scrolling trade tape, deduped by tick ts, fed by the same SSE stream as the price rows above.

const TL_MAX_LINES = 50;
let _tlLog = [];
const _tlLastTs = {};

function _tlObserve(code, row) {
  if (!row || !row.ts || _tlLastTs[code] === row.ts) return;
  _tlLastTs[code] = row.ts;

  _tlLog.unshift({ code, price: row.price, qty: row.qty, ts: row.ts });
  if (_tlLog.length > TL_MAX_LINES) _tlLog.length = TL_MAX_LINES;
  _tlRender();
}

function _tlRender() {
  const container = document.getElementById("tlContainer");
  if (!container) return;
  if (!_tlLog.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No prints yet.</div>';
    return;
  }
  container.innerHTML = _tlLog.map(line => {
    const time = new Date(line.ts).toLocaleTimeString();
    const qty = line.qty != null ? `${line.qty} units` : "unknown qty";
    return `<div>${time} — ${_wlSafe(line.code)} traded ${qty} at ${line.price}</div>`;
  }).join("");
}

// Re-apply the Screener/Live warrant choice saved before the last reload, the
// same way common.js restores the top-level tab. The HTML renders Screener
// active, so this only acts when Live warrant was the last view.
function restoreScannerSub() {
  if (_readView("ws_scannerSub") !== "live") return;
  const btn = document.getElementById("scsub-btn-live");
  if (btn) switchScannerSub("live", btn);
}

document.addEventListener("DOMContentLoaded", () => {
  onBrokerChange();
  restoreScannerSub();
});
