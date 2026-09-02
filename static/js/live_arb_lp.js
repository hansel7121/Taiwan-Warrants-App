// Live Arb LP sub-tab: polls /live_arb_lp_data every 500ms, same convention
// as live_arb.js's Direct Match sub-tab — detection/logging runs server-side
// on its own self-paced loop (services/live_arb.py), this file only samples
// the current snapshot for display. A full LP scan is far more expensive
// than Direct Match's (~90ms measured vs ~8ms), so unlike Direct Match this
// subtab does not track/update on every tick — see the tab's own copy for
// why. Structure counts here are always small (TSMC-only, at most one
// structure per option expiry), so a full rebuild each poll is simple and
// cheap, same reasoning as live_arb.js's active-hit table.

let _lalpLoaded = false;
let _lalpPollTimer = null;
let _lalpInFlight = false;
let _lalpLastLoggedCount = -1;

function _lalpMoney(v) {
  return v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function _lalpStructureSummary(legs) {
  const nl = (legs || []).filter(l => l.side === "long").length;
  const ns = (legs || []).length - nl;
  const codes = (legs || []).slice(0, 3).map(l => l.code).join(", ");
  const more = (legs || []).length > 3 ? ` +${legs.length - 3}` : "";
  return `${nl} long &middot; ${ns} short<div style="font-size:11px;color:var(--muted)">${codes}${more}</div>`;
}

async function loadLiveArbLpOnce() {
  if (_lalpLoaded) return;
  _lalpLoaded = true;
  _lalpPollTimer = setInterval(_lalpPoll, 500);
  _lalpPoll();
}

function _lalpRenderActive(structures) {
  const tbody = document.getElementById("lalp-active-tbody");
  const empty = document.getElementById("lalp-active-empty");
  if (!tbody) return;
  tbody.innerHTML = "";
  empty.style.display = structures.length ? "none" : "";
  structures.forEach(s => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.horizon_dte}d</td>
      <td>${_lalpStructureSummary(s.legs)}</td>
      <td style="text-align:right">${_lalpMoney(s.net_credit)}</td>
      <td style="text-align:right"><div class="put" style="font-weight:700">${_lalpMoney(s.guaranteed_profit)}</div></td>
      <td style="text-align:right">${s.return_pct === null || s.return_pct === undefined ? "—" : s.return_pct + "%"}</td>
    `;
    tbody.appendChild(tr);
  });
}

function _lalpRenderTrades(trades) {
  const tbody = document.getElementById("lalp-log-tbody");
  const empty = document.getElementById("lalp-log-empty");
  if (!tbody) return;
  tbody.innerHTML = "";
  empty.style.display = trades.length ? "none" : "";
  trades.forEach(t => {
    const tr = document.createElement("tr");
    const time = t.detected_at ? new Date(t.detected_at).toLocaleTimeString() : "—";
    tr.innerHTML = `
      <td>${time}</td>
      <td>${t.horizon_dte}d</td>
      <td>${_lalpStructureSummary(t.legs)}</td>
      <td style="text-align:right">${_lalpMoney(t.net_credit)}</td>
      <td style="text-align:right">${_lalpMoney(t.guaranteed_profit)}</td>
      <td style="text-align:right">${t.return_pct === null || t.return_pct === undefined ? "—" : t.return_pct + "%"}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function _lalpFetchTrades() {
  try {
    const d = await apiJson("/live_arb_lp_trades");
    _lalpRenderTrades(d.trades || []);
  } catch (e) {
    // The status line already shows poll errors from _lalpPoll.
  }
}

function _lalpPoll() {
  if (_lalpInFlight) return;
  const tab = document.getElementById("tab-live-arb");
  const sub = document.getElementById("lasub-lp");
  if (!tab || !tab.classList.contains("active")) return;
  if (!sub || sub.style.display === "none") return;
  _lalpInFlight = true;
  apiJson("/live_arb_lp_data")
    .then(d => {
      _lalpInFlight = false;
      const statusEl = document.getElementById("lalp-status");
      if (statusEl) {
        statusEl.textContent = d.session_error
          ? "error: " + d.session_error
          : (d.enabled ? `running — ${d.trade_date || ""}` : "stopped");
      }
      document.getElementById("lalp-active-count").textContent = d.active_count;
      document.getElementById("lalp-logged-count").textContent = d.logged_count_today;
      _lalpRenderActive(d.active_structures || []);
      if (d.logged_count_today !== _lalpLastLoggedCount) {
        _lalpLastLoggedCount = d.logged_count_today;
        _lalpFetchTrades();
      }
    })
    .catch(e => {
      _lalpInFlight = false;
      const statusEl = document.getElementById("lalp-status");
      if (statusEl) statusEl.textContent = "server unreachable: " + (e && e.message ? e.message : e);
    });
}

async function _lalpAction(btnId, endpoint, verb) {
  const btn = document.getElementById(btnId);
  const statusEl = document.getElementById("lalp-status");
  btn.disabled = true;
  try {
    await apiJson(endpoint, { method: "POST", headers: { "Content-Type": "application/json" } });
    _lalpPoll();
  } catch (e) {
    if (statusEl) statusEl.textContent = verb + " failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

function startLiveArbLp() { return _lalpAction("lalp-start-btn", "/start_live_arb_lp", "start"); }
function stopLiveArbLp() { return _lalpAction("lalp-stop-btn", "/stop_live_arb_lp", "stop"); }
