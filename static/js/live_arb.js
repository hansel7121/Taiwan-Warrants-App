// Live Arb tab: polls /live_arb_data every 500ms, same convention as
// live_warrant.js/live_options.js — the actual arb detection + logging runs
// server-side on a faster independent timer (services/live_arb.py), this
// file only samples the current snapshot for display. The active-hit and
// logged-trade tables are small (a handful of rows at most, TSMC-only), so
// unlike live_warrant.js there's no need for cell-level diff-patching — a
// full rebuild each poll is simple and cheap at this size.

let _laLoaded = false;
let _laPollTimer = null;
let _laInFlight = false;
let _laLastLoggedCount = -1;

function _laMoney(v) {
  return v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 4 });
}

async function loadLiveArbOnce() {
  if (_laLoaded) return;
  _laLoaded = true;
  _laPollTimer = setInterval(_laPoll, 500);
  _laPoll();
}

function _laRenderActive(hits) {
  const tbody = document.getElementById("la-active-tbody");
  const empty = document.getElementById("la-active-empty");
  if (!tbody) return;
  tbody.innerHTML = "";
  empty.style.display = hits.length ? "none" : "";
  hits.forEach(h => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${h.warrant_code}<div style="font-size:11px;color:var(--muted)">${h.warrant_name || ""}</div></td>
      <td>${h.type || "—"}</td>
      <td>${h.option_code}</td>
      <td>${_laMoney(h.warrant_strike)} / ${_laMoney(h.opt_strike)}</td>
      <td>${h.warrant_dte} / ${h.opt_dte}</td>
      <td>${_laMoney(h.warrant_ask)}</td>
      <td>${_laMoney(h.opt_bid)}</td>
      <td>${_laMoney(h.price_diff)}</td>
      <td>${h.price_diff_pct === null || h.price_diff_pct === undefined ? "—" : h.price_diff_pct + "%"}</td>
      <td>${h.riskless ? "yes" : "no"}</td>
    `;
    tbody.appendChild(tr);
  });
}

function _laRenderTrades(trades) {
  const tbody = document.getElementById("la-log-tbody");
  const empty = document.getElementById("la-log-empty");
  if (!tbody) return;
  tbody.innerHTML = "";
  empty.style.display = trades.length ? "none" : "";
  trades.forEach(t => {
    const tr = document.createElement("tr");
    const time = t.detected_at ? new Date(t.detected_at).toLocaleTimeString() : "—";
    tr.innerHTML = `
      <td>${time}</td>
      <td>${t.warrant_code}<div style="font-size:11px;color:var(--muted)">${t.warrant_name || ""}</div></td>
      <td>${t.option_contract}</td>
      <td>${_laMoney(t.warrant_ask)}</td>
      <td>${_laMoney(t.opt_bid)}</td>
      <td>${_laMoney(t.price_diff)}</td>
      <td>${t.price_diff_pct === null || t.price_diff_pct === undefined ? "—" : t.price_diff_pct + "%"}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function _laFetchTrades() {
  try {
    const d = await apiJson("/live_arb_trades");
    _laRenderTrades(d.trades || []);
  } catch (e) {
    // The status line already shows poll errors from _laPoll; a trades-list
    // fetch failure alone isn't worth a second error surface.
  }
}

function _laPoll() {
  if (_laInFlight) return;
  const tab = document.getElementById("tab-live-arb");
  if (!tab || !tab.classList.contains("active")) return;
  _laInFlight = true;
  apiJson("/live_arb_data")
    .then(d => {
      _laInFlight = false;
      const statusEl = document.getElementById("la-status");
      if (statusEl) {
        statusEl.textContent = d.session_error
          ? "error: " + d.session_error
          : (d.enabled ? `running — ${d.trade_date || ""}` : "stopped");
      }
      document.getElementById("la-active-count").textContent = d.active_count;
      document.getElementById("la-logged-count").textContent = d.logged_count_today;
      _laRenderActive(d.active_hits || []);
      if (d.logged_count_today !== _laLastLoggedCount) {
        _laLastLoggedCount = d.logged_count_today;
        _laFetchTrades();
      }
    })
    .catch(e => {
      _laInFlight = false;
      const statusEl = document.getElementById("la-status");
      if (statusEl) statusEl.textContent = "server unreachable: " + (e && e.message ? e.message : e);
    });
}

async function _laAction(btnId, endpoint, verb) {
  const btn = document.getElementById(btnId);
  const statusEl = document.getElementById("la-status");
  btn.disabled = true;
  try {
    await apiJson(endpoint, { method: "POST", headers: { "Content-Type": "application/json" } });
    _laPoll();
  } catch (e) {
    if (statusEl) statusEl.textContent = verb + " failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

function startLiveArb() { return _laAction("la-start-btn", "/start_live_arb", "start"); }
function stopLiveArb() { return _laAction("la-stop-btn", "/stop_live_arb", "stop"); }
