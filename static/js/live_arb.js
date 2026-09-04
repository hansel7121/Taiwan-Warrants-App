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

// "Last received tick: option CDA06500A4 (TSMC Call) bid 3.20 / ask 3.40,
// 2.1s ago — arb is up to date". A debug line, not a status line: `up_to_date`
// answers "does the currently-displayed scan actually reflect the latest
// tick" by comparing a freshly-fetched tick-seq against the seq the last
// scan iteration ran against (services/live_arb.py's `_tick_and_freshness`)
// — this can legitimately read NOT up to date right after Stop (ticks keep
// arriving, the scan loop just isn't consuming them any more), which is the
// whole point of surfacing it rather than assuming the background loop
// always keeps up.
function _laFormatTickLine(d) {
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

async function loadLiveArbOnce() {
  if (_laLoaded) return;
  _laLoaded = true;
  _laPollTimer = setInterval(_laPoll, 500);
  _laPoll();
}

// Own class (.lasub-btn/.lasub-content), not Portfolio's .pfsub-btn/
// .pfsub-content: those are selected globally by switchPfSub, so sharing
// them would let switching Portfolio's sub-tab blank out whichever Live Arb
// sub-tab was open.
function switchLaSub(sub, btn) {
  document.querySelectorAll("#tab-live-arb .lasub-content").forEach(el => el.style.display = "none");
  document.querySelectorAll("#tab-live-arb .lasub-btn").forEach(el => el.classList.remove("active"));
  document.getElementById("lasub-" + sub).style.display = "block";
  btn.classList.add("active");
  if (sub === "lp") loadLiveArbLpOnce();
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
      const tickEl = document.getElementById("la-last-tick");
      if (tickEl) tickEl.innerHTML = _laFormatTickLine(d);
      _laRenderActive(d.active_hits || []);
      if (d.logged_count_today !== _laLastLoggedCount) {
        _laLastLoggedCount = d.logged_count_today;
        _laFetchTrades();
      }
      _laPollRecordStatus();
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

// ── Tick-by-tick CSV recorder (services/live_tick_log.py) ──────────────────
// One shared recorder behind /start_live_tick_log, /stop_live_tick_log,
// /live_tick_log_csv — Direct Match and the LP subtab (live_arb_lp.js) both
// point at the same backend state, so starting it from either subtab records
// for both. Status is refreshed as part of the normal 500ms poll below.

function _laFormatRecordLine(s) {
  if (!s) return "not recording";
  const rows = (s.rows_logged || 0).toLocaleString();
  return s.active
    ? `<span style="color:var(--put)">recording</span> — ${rows} rows so far`
    : (s.rows_logged ? `stopped — ${rows} rows captured` : "not recording");
}

function _laApplyRecordStatus(s) {
  const btn = document.getElementById("la-record-btn");
  const statusEl = document.getElementById("la-record-status");
  if (btn) btn.textContent = s && s.active ? "Stop Recording" : "Record";
  if (statusEl) statusEl.innerHTML = _laFormatRecordLine(s);
}

async function _laPollRecordStatus() {
  try {
    _laApplyRecordStatus(await apiJson("/live_tick_log_status"));
  } catch (e) {
    // best-effort — the main status line already surfaces server-unreachable
  }
}

async function _laToggleRecord() {
  const btn = document.getElementById("la-record-btn");
  if (btn) btn.disabled = true;
  try {
    const s = await apiJson("/live_tick_log_status");
    const endpoint = s.active ? "/stop_live_tick_log" : "/start_live_tick_log";
    _laApplyRecordStatus(await apiJson(endpoint, { method: "POST", headers: { "Content-Type": "application/json" } }));
  } catch (e) {
    const statusEl = document.getElementById("la-record-status");
    if (statusEl) statusEl.textContent = "toggle failed: " + (e.message || e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _laDownloadRecordCSV() {
  const statusEl = document.getElementById("la-record-status");
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
