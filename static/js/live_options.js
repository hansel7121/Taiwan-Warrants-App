// Live Options tab: polls /live_options_data every 500ms and renders TSMC's
// option chain as expiry tabs over a put-left/strike-middle/call-right
// strike ladder. No pricing computation anywhere in this file — it only
// ever renders whatever raw best-bid/best-ask the payload carries.
//
// Reuses _lwSetText/_lwLevelText/_lwNum/_lwAge from live_warrant.js (both
// scripts are always loaded together, and those four are pure globals with
// no hardcoded element IDs). Does NOT reuse _lwSessionAction — it hardcodes
// "live-status", so a local _loSessionAction hardcodes "lo-status" instead
// rather than touching the already-shipped warrant tab.

let _loLoaded = false;
let _loPollTimer = null;
let _loInFlight = false;
let _loGrouped = {};        // expiry -> { strike -> {put, call} }
let _loExpiries = [];       // sorted ISO date strings
let _loActiveExpiry = null; // client-side only — switching tabs never re-fetches
let _loRowsByStrike = {};   // strike -> {tr, cells} for the CURRENTLY VISIBLE expiry only

async function loadLiveOptionsOnce() {
  if (_loLoaded) return;
  _loLoaded = true;
  _loPollTimer = setInterval(_loPoll, 500);
  _loPoll();
}

function _loGroup(contracts) {
  const grouped = {};
  (contracts || []).forEach(c => {
    if (!c.expiry) return;
    grouped[c.expiry] = grouped[c.expiry] || {};
    grouped[c.expiry][c.strike] = grouped[c.expiry][c.strike] || {};
    grouped[c.expiry][c.strike][c.is_put ? "put" : "call"] = c;
  });
  return grouped;
}

// A contract the backend could not fully set up: still tracked, just not
// streaming yet. Same convention as live_warrant.js's _lwDegraded.
function _loDegraded(c) {
  if (!c) return "";
  if (c.pending) return c.error ? `pending — ${c.error}` : "pending — not subscribed yet";
  return c.error || "";
}

function _loBuildStrikeRow() {
  const tr = document.createElement("tr");
  tr.className = "lo-row";

  const putBidTd = document.createElement("td"); putBidTd.className = "live-term bid";
  const putAskTd = document.createElement("td"); putAskTd.className = "live-term ask";
  const putRemoveTd = document.createElement("td");
  const putRemoveBtn = document.createElement("button");
  putRemoveBtn.className = "sm"; putRemoveBtn.textContent = "×"; putRemoveBtn.title = "Remove this put";
  putRemoveTd.appendChild(putRemoveBtn);

  const strikeTd = document.createElement("td");
  strikeTd.className = "lo-strike";

  const callRemoveTd = document.createElement("td");
  const callRemoveBtn = document.createElement("button");
  callRemoveBtn.className = "sm"; callRemoveBtn.textContent = "×"; callRemoveBtn.title = "Remove this call";
  callRemoveTd.appendChild(callRemoveBtn);
  const callBidTd = document.createElement("td"); callBidTd.className = "live-term bid";
  const callAskTd = document.createElement("td"); callAskTd.className = "live-term ask";

  [putBidTd, putAskTd, putRemoveTd, strikeTd, callRemoveTd, callBidTd, callAskTd]
    .forEach(td => tr.appendChild(td));

  return {
    tr,
    cells: { putBid: putBidTd, putAsk: putAskTd, strike: strikeTd, callBid: callBidTd, callAsk: callAskTd },
    putRemoveBtn, callRemoveBtn,
  };
}

// Title tooltip + a dimmed style on a side's two price cells when that
// side's book came from the REST seed and hasn't ticked yet — same
// "snapshot, Xs old" wording live_warrant.js's dedicated Age column uses
// (_lwAge, reused here as a global), just surfaced as a tooltip instead of
// its own column since the put/strike/call ladder has no room for one.
function _loApplySnapshotState(bidTd, askTd, c) {
  const age = c ? _lwAge(c) : "";
  const isSnapshot = !!(c && c.src === "rest");
  [bidTd, askTd].forEach(td => {
    td.title = age;
    td.classList.toggle("lo-snapshot", isSnapshot);
  });
}

function _loPatchStrikeRow(el, strike, putC, callC) {
  _lwSetText(el.cells.strike, _lwNum(strike, 2));

  const putBest = (putC && putC.best) || {};
  const callBest = (callC && callC.best) || {};
  _lwSetText(el.cells.putBid, _lwLevelText(putBest.bid, putBest.bid_size));
  _lwSetText(el.cells.putAsk, _lwLevelText(putBest.ask, putBest.ask_size));
  _lwSetText(el.cells.callBid, _lwLevelText(callBest.bid, callBest.bid_size));
  _lwSetText(el.cells.callAsk, _lwLevelText(callBest.ask, callBest.ask_size));
  _loApplySnapshotState(el.cells.putBid, el.cells.putAsk, putC);
  _loApplySnapshotState(el.cells.callBid, el.cells.callAsk, callC);

  el.putRemoveBtn.style.visibility = putC ? "visible" : "hidden";
  el.putRemoveBtn.onclick = putC ? () => removeLiveOption(putC.code) : null;
  el.callRemoveBtn.style.visibility = callC ? "visible" : "hidden";
  el.callRemoveBtn.onclick = callC ? () => removeLiveOption(callC.code) : null;

  const degraded = _loDegraded(putC) || _loDegraded(callC);
  if (el.degraded !== degraded) {
    el.tr.classList.toggle("live-degraded", !!degraded);
    el.tr.title = degraded;
    el.degraded = degraded;
  }
}

function _loRenderExpiryTabs() {
  const host = document.getElementById("lo-expiry-tabs");
  if (!host) return;
  host.innerHTML = "";
  _loExpiries.forEach(expiry => {
    const btn = document.createElement("button");
    btn.className = "pfsub-btn" + (expiry === _loActiveExpiry ? " active" : "");
    btn.textContent = expiry;
    btn.onclick = () => switchLiveOptionsExpiry(expiry);
    host.appendChild(btn);
  });
}

function switchLiveOptionsExpiry(expiry) {
  _loActiveExpiry = expiry;
  _loRowsByStrike = {};   // different strike set — fresh table, no stale carry-over
  const tbody = document.getElementById("lo-tbody");
  if (tbody) tbody.innerHTML = "";
  _loRenderExpiryTabs();
  _loRenderChain();
}

function _loRenderChain() {
  const tbody = document.getElementById("lo-tbody");
  if (!tbody) return;
  if (!_loActiveExpiry) { tbody.innerHTML = ""; return; }

  const strikes = _loGrouped[_loActiveExpiry] || {};
  const sortedStrikes = Object.keys(strikes).map(Number).sort((a, b) => a - b);

  const seen = {};
  sortedStrikes.forEach(strike => {
    seen[strike] = true;
    let el = _loRowsByStrike[strike];
    if (!el) {
      el = _loRowsByStrike[strike] = _loBuildStrikeRow();
      tbody.appendChild(el.tr);
    }
    const pair = strikes[strike] || {};
    _loPatchStrikeRow(el, strike, pair.put || null, pair.call || null);
  });

  Object.keys(_loRowsByStrike).forEach(strike => {
    if (!seen[strike]) {
      _loRowsByStrike[strike].tr.remove();
      delete _loRowsByStrike[strike];
    }
  });
}

function _loStatusLine(d) {
  const dot = d.connected ? "ok" : "warn";
  const connSummary = d.connections
    .map(c => `#${c.index}:${c.state}(${c.subs}/${d.max_subs / d.max_connections})`)
    .join(" ");
  let html = `<span class="dot ${dot}"></span><b>${d.connected ? "connected" : "not connected"}</b>`
    + ` &nbsp;|&nbsp; subscriptions <b>${d.subs}/${d.max_subs}</b>`
    + (connSummary ? ` &nbsp;|&nbsp; ${connSummary}` : "");
  if (d.pending) html += ` &nbsp;|&nbsp; <span class="down">${d.pending} pending</span>`;
  if (d.errors) {
    const kinds = (d.error_kinds || []).join("; ");
    html += ` &nbsp;|&nbsp; <span class="down" title="${kinds}">${d.errors} with errors</span>`;
    if (kinds) html += ` <span class="muted">(${kinds})</span>`;
  }
  if (d.session_error) html += ` &nbsp;|&nbsp; <span class="down">${d.session_error}</span>`;
  return html;
}

function _loRender(d) {
  const statusEl = document.getElementById("lo-status");
  if (statusEl) statusEl.innerHTML = _loStatusLine(d);

  _loGrouped = _loGroup(d.contracts);
  _loExpiries = (d.expiries || []).slice().sort();
  if (!_loActiveExpiry || !_loExpiries.includes(_loActiveExpiry)) {
    _loActiveExpiry = _loExpiries[0] || null;
    _loRowsByStrike = {};
    const tbody = document.getElementById("lo-tbody");
    if (tbody) tbody.innerHTML = "";
  }
  _loRenderExpiryTabs();
  _loRenderChain();

  const emptyEl = document.getElementById("lo-empty");
  if (emptyEl) emptyEl.style.display = (d.contracts || []).length === 0 ? "block" : "none";
}

function _loPoll() {
  if (_loInFlight) return;
  const tab = document.getElementById("tab-live-options");
  if (!tab || !tab.classList.contains("active")) return;
  _loInFlight = true;
  apiJson("/live_options_data")
    .then(d => { _loInFlight = false; _loRender(d); })
    .catch(e => {
      _loInFlight = false;
      const statusEl = document.getElementById("lo-status");
      if (statusEl) statusEl.textContent = "server unreachable: " + (e && e.message ? e.message : e);
    });
}

async function loadLiveOptionsChain() {
  const btn = document.getElementById("lo-load-btn");
  const statusEl = document.getElementById("lo-load-status");
  btn.disabled = true;
  // Opening however many pool connections the discovered chain needs is the
  // slow part (each a real blocking SDK login) — same wording pattern as
  // runLiveWarrantScan()'s whole-chain scan.
  if (statusEl) statusEl.textContent = "Discovering and subscribing the TSMC chain — this can take a while…";
  try {
    const r = await apiJson("/load_live_options_chain", {
      method: "POST", headers: { "Content-Type": "application/json" },
    });
    if (statusEl) statusEl.textContent =
      `chain ${r.chain} · +${r.added} added · ${r.failed} pending retry` +
      (r.parse_failures ? ` · ${r.parse_failures} unparsable` : "");
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || String(e);
  } finally {
    btn.disabled = false;
  }
}

async function removeLiveOption(code) {
  try {
    await apiJson("/remove_live_option", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch (e) {
    const statusEl = document.getElementById("lo-status");
    if (statusEl) statusEl.textContent = e.message || String(e);
  }
}

async function retryLiveOptions() {
  const statusEl = document.getElementById("lo-status");
  const btn = document.getElementById("lo-retry-btn");
  btn.disabled = true;
  try {
    const r = await apiJson("/retry_live_options", {
      method: "POST", headers: { "Content-Type": "application/json" },
    });
    if (statusEl) statusEl.textContent = `retried: ${r.subscribed} subscribed, ${r.pending} pending`;
  } catch (e) {
    if (statusEl) statusEl.textContent = "retry failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

async function _loSessionAction(btnId, endpoint, verb) {
  const btn = document.getElementById(btnId);
  const statusEl = document.getElementById("lo-status");
  btn.disabled = true;
  btn.textContent = verb + "ing…";
  try {
    await apiJson(endpoint, { method: "POST", headers: { "Content-Type": "application/json" } });
  } catch (e) {
    if (statusEl) statusEl.textContent = verb + " failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = verb;
  }
}
function reconnectLiveOptions() { return _loSessionAction("lo-reconnect-btn", "/reconnect_live_options", "Reconnect"); }
function connectLiveOptions() { return _loSessionAction("lo-connect-btn", "/connect_live_options", "Connect"); }
function disconnectLiveOptions() { return _loSessionAction("lo-disconnect-btn", "/disconnect_live_options", "Disconnect"); }
