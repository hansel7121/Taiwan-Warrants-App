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

// Same debug line as live_arb.js's _laFormatTickLine (Direct Match sub-tab)
// — duplicated rather than shared, same convention as _laMoney/_lalpMoney
// above. `up_to_date` here compares against THIS subtab's own last-scanned
// seq (_lp_last_seq), independent of Direct Match's — the LP scan is far
// slower (~90ms vs ~8ms) and self-paced rather than tick-synchronous, so it
// can genuinely fall behind a burst of ticks even while Direct Match is
// caught up.
function _lalpFormatTickLine(d) {
  const t = d.last_tick;
  if (!t) return "Last received tick: none yet";
  const bid = t.bid === null || t.bid === undefined ? "—" : Number(t.bid).toFixed(2);
  const ask = t.ask === null || t.ask === undefined ? "—" : Number(t.ask).toFixed(2);
  const secs = t.seconds_ago;
  const age = secs < 90 ? `${secs.toFixed(1)}s` : `${Math.round(secs / 60)}m`;
  const label = t.name && t.name !== t.code ? `${t.code} (${t.name})` : t.code;
  const freshness = d.up_to_date
    ? `<span style="color:var(--put)">arb is up to date</span>`
    : `<span class="down">arb is NOT up to date</span>`;
  return `Last received tick: ${t.kind} ${label} bid ${bid} / ask ${ask}, ${age} ago — ${freshness}`;
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
      const tickEl = document.getElementById("lalp-last-tick");
      if (tickEl) tickEl.innerHTML = _lalpFormatTickLine(d);
      _lalpRenderActive(d.active_structures || []);
      if (d.logged_count_today !== _lalpLastLoggedCount) {
        _lalpLastLoggedCount = d.logged_count_today;
        _lalpFetchTrades();
      }
      _lalpPollRecordStatus();
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

// ── Tick-by-tick CSV recorder (services/live_tick_log.py) ──────────────────
// Same shared recorder as live_arb.js's Direct Match copy — duplicated here
// rather than factored out, same convention as this file's other _lalp*
// mirrors of _la* functions. Starting/stopping/downloading from either
// subtab acts on the one global recorder.

function _lalpFormatRecordLine(s) {
  if (!s) return "not recording";
  const rows = (s.rows_logged || 0).toLocaleString();
  return s.active
    ? `<span style="color:var(--put)">recording</span> — ${rows} rows so far`
    : (s.rows_logged ? `stopped — ${rows} rows captured` : "not recording");
}

function _lalpApplyRecordStatus(s) {
  const btn = document.getElementById("lalp-record-btn");
  const statusEl = document.getElementById("lalp-record-status");
  if (btn) btn.textContent = s && s.active ? "Stop Recording" : "Record";
  if (statusEl) statusEl.innerHTML = _lalpFormatRecordLine(s);
}

async function _lalpPollRecordStatus() {
  try {
    _lalpApplyRecordStatus(await apiJson("/live_tick_log_status"));
  } catch (e) {
    // best-effort — the main status line already surfaces server-unreachable
  }
}

async function _lalpToggleRecord() {
  const btn = document.getElementById("lalp-record-btn");
  if (btn) btn.disabled = true;
  try {
    const s = await apiJson("/live_tick_log_status");
    const endpoint = s.active ? "/stop_live_tick_log" : "/start_live_tick_log";
    _lalpApplyRecordStatus(await apiJson(endpoint, { method: "POST", headers: { "Content-Type": "application/json" } }));
  } catch (e) {
    const statusEl = document.getElementById("lalp-record-status");
    if (statusEl) statusEl.textContent = "toggle failed: " + (e.message || e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _lalpDownloadRecordCSV() {
  const statusEl = document.getElementById("lalp-record-status");
  try {
    const res = await api("/live_tick_log_csv");
    if (!res.ok) {
      if (statusEl) statusEl.textContent = "download failed: no ticks recorded yet";
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "tsmc_ticks.csv"; a.click();
  } catch (e) {
    if (statusEl) statusEl.textContent = "download failed: " + (e.message || e);
  }
}
