// Live Arb → EOD Replay sub-tab: browses the end-of-day replay results
// (scripts/eod_arb_replay.py → eod_arb_* tables) day by day via
// /eod_arb_dates and /eod_arb_episodes, re-runs a day via /eod_arb_run, and
// downloads a day's tick file via /eod_tick_csv. Static data — no live poll,
// except while a replay is running.

let _eodLoaded = false;
let _eodRunPoll = null;
const _eodLpRows = {};

function _eodEsc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function _eodNum(v, digits) {
  return v === null || v === undefined ? "—"
    : Number(v).toLocaleString(undefined, { minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 });
}

function _eodTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-GB", { timeZone: "Asia/Taipei", hour12: false })
    + "." + String(new Date(iso).getMilliseconds()).padStart(3, "0");
}

function _eodDuration(s, openAtClose) {
  if (s === null || s === undefined) return "—";
  const n = Number(s);
  const txt = n < 60 ? `${n.toFixed(n < 10 ? 1 : 0)}s` : n < 3600 ? `${Math.floor(n / 60)}m ${Math.round(n % 60)}s`
    : `${Math.floor(n / 3600)}h ${Math.round((n % 3600) / 60)}m`;
  return openAtClose ? `${txt} <span style="color:var(--muted)" title="Still open at the 13:30 close">(at close)</span>` : txt;
}

function _eodLegs(legs) {
  return (legs || []).map(l =>
    `<div style="white-space:nowrap"><span class="${l.side === "long" ? "call" : "put"}">${l.side === "long" ? "Buy" : "Sell"}</span>
      ${_eodNum(l.lots)} ${_eodEsc(l.lot_label || (l.kind === "warrant" ? "張" : "口"))}
      ${_eodEsc(l.code)} ${_eodEsc(l.type || "")} ${_eodNum(l.strike, 0)} @ ${_eodNum(l.quote, 2)}</div>`).join("");
}

async function loadEodReplayOnce() {
  if (_eodLoaded) return;
  _eodLoaded = true;
  await _eodRefresh();
}

async function _eodRefresh() {
  const status = document.getElementById("eod-status");
  const sel = document.getElementById("eod-date");
  try {
    const d = await apiJson("/eod_arb_dates");
    const keep = sel.value;
    const days = new Map();
    (d.runs || []).forEach(r => days.set(r.trade_date, r));
    (d.files || []).forEach(f => { if (!days.has(f.date)) days.set(f.date, { trade_date: f.date, status: "not replayed" }); });
    const sorted = [...days.keys()].sort().reverse();
    sel.innerHTML = sorted.map(k => {
      const r = days.get(k);
      const tag = r.status === "ok" ? `${r.n_direct || 0} D / ${r.n_lp || 0} LP` : r.status;
      return `<option value="${k}">${k} — ${_eodEsc(tag)}</option>`;
    }).join("");
    if (!sorted.length) {
      status.textContent = "No recorded days yet — the first one appears after the next trading day's 13:40 replay.";
      return;
    }
    sel.value = sorted.includes(keep) ? keep : sorted[0];
    await _eodLoadDay();
  } catch (e) {
    status.textContent = "load failed: " + (e.message || e);
  }
}

async function _eodLoadDay() {
  const day = document.getElementById("eod-date").value;
  const status = document.getElementById("eod-status");
  if (!day) return;
  status.textContent = "loading…";
  try {
    const d = await apiJson(`/eod_arb_episodes?date=${encodeURIComponent(day)}`);
    const run = d.run;
    if (!run) status.textContent = "Recorded but not replayed yet — click Re-run.";
    else if (run.status === "running") status.textContent = "Replay running…";
    else if (run.status === "error") status.textContent = "Replay failed: " + (run.error || "unknown error");
    else if (run.status === "no_ticks") status.textContent = "No ticks recorded this day (holiday or recorder off).";
    else status.textContent = `Replayed ${_eodNum(run.n_ticks)} ticks in ${_eodNum(run.runtime_s, 0)}s`
      + ` · ${_eodNum(run.n_lp_solves)} full LP solves`;
    document.getElementById("eod-direct-count").textContent = d.direct.length;
    document.getElementById("eod-lp-count").textContent = d.lp.length;
    document.getElementById("eod-tick-count").textContent = run ? _eodNum(run.n_ticks) : "—";
    _eodRenderDirect(d.direct);
    _eodRenderLp(d.lp);
    _eodWatchRun(d.replay_running || (run && run.status === "running"));
  } catch (e) {
    status.textContent = "load failed: " + (e.message || e);
  }
}

function _eodRenderDirect(rows) {
  const tbody = document.getElementById("eod-direct-tbody");
  document.getElementById("eod-direct-empty").style.display = rows.length ? "none" : "";
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${_eodTime(r.started_at)}</td><td>${_eodTime(r.ended_at)}</td>
      <td style="text-align:right">${_eodDuration(r.duration_s, r.open_at_close)}</td>
      <td>${_eodEsc(r.warrant_code)}<div style="font-size:11px;color:var(--muted)">${_eodEsc(r.warrant_name)}</div></td>
      <td>${_eodEsc(r.option_code)}</td>
      <td class="${r.type === "Call" ? "call" : "put"}">${_eodEsc(r.type)}</td>
      <td>${_eodNum(r.warrant_strike, 0)} / ${_eodNum(r.opt_strike, 0)}</td>
      <td>${_eodNum(r.warrant_dte)} / ${_eodNum(r.opt_dte)}</td>
      <td style="text-align:right">${_eodNum(r.warrant_ask, 2)} (${_eodNum(r.warrant_ask_size)})</td>
      <td style="text-align:right">${_eodNum(r.opt_bid, 2)} (${_eodNum(r.opt_bid_size)})</td>
      <td style="text-align:right">${_eodNum(r.price_diff, 2)} <span style="color:var(--muted)">(${_eodNum(r.price_diff_pct, 1)}%)</span></td>
      <td style="text-align:right" title="at ${_eodTime(r.peak_at)}">${_eodNum(r.peak_price_diff, 2)}</td>
    </tr>`).join("");
}

function _eodRenderLp(rows) {
  const tbody = document.getElementById("eod-lp-tbody");
  document.getElementById("eod-lp-empty").style.display = rows.length ? "none" : "";
  rows.forEach(r => { _eodLpRows[r.id] = r; });
  tbody.innerHTML = rows.map(r => `
    <tr style="cursor:pointer" onclick="_eodToggleLp(this, '${_eodEsc(r.id)}')" title="Click for the peak structure and every leg seen">
      <td>${_eodTime(r.started_at)}</td><td>${_eodTime(r.ended_at)}</td>
      <td style="text-align:right">${_eodDuration(r.duration_s, r.open_at_close)}</td>
      <td>${_eodNum(r.horizon_dte)}d</td>
      <td style="font-size:12px">${_eodLegs(r.legs)}</td>
      <td style="text-align:right">${_eodNum(r.net_credit)}</td>
      <td style="text-align:right"><span class="put" style="font-weight:700">${_eodNum(r.guaranteed_profit)}</span></td>
      <td style="text-align:right">${r.return_pct === null || r.return_pct === undefined ? "—" : r.return_pct + "%"}</td>
      <td style="text-align:right" title="at ${_eodTime(r.peak_at)}">${_eodNum(r.peak_guaranteed_profit)}</td>
    </tr>`).join("");
}

function _eodToggleLp(tr, id) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("eod-lp-detail")) { next.remove(); return; }
  const r = _eodLpRows[id];
  if (!r) return;
  const detail = document.createElement("tr");
  detail.className = "eod-lp-detail";
  detail.innerHTML = `<td colspan="9" style="font-size:12px;background:var(--panel, transparent)">
    <div style="margin:4px 0 6px"><b>Peak structure</b> at ${_eodTime(r.peak_at)} — guaranteed ${_eodNum(r.peak_guaranteed_profit)}
      (${r.peak_return_pct === null || r.peak_return_pct === undefined ? "—" : r.peak_return_pct + "%"})</div>
    ${_eodLegs(r.peak_legs)}
    <div style="margin-top:6px;color:var(--muted)">Every instrument used during the episode: ${_eodEsc((r.leg_codes || "").split("|").join(", "))}</div>
  </td>`;
  tr.after(detail);
}

function _eodWatchRun(running) {
  if (running && !_eodRunPoll) {
    _eodRunPoll = setInterval(_eodLoadDay, 5000);
  } else if (!running && _eodRunPoll) {
    clearInterval(_eodRunPoll);
    _eodRunPoll = null;
    _eodRefresh();
  }
}

async function _eodRerun() {
  const day = document.getElementById("eod-date").value;
  if (!day) return;
  if (!confirm(`Replay ${day} again? Its logged episodes are replaced with the new run's.`)) return;
  const btn = document.getElementById("eod-rerun-btn");
  const status = document.getElementById("eod-status");
  btn.disabled = true;
  try {
    const d = await apiJson("/eod_arb_run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ date: day }),
    });
    status.textContent = d.ok ? "Replay started…" : (d.error || "could not start");
    if (d.ok) _eodWatchRun(true);
  } catch (e) {
    status.textContent = "re-run failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

async function _eodDownloadTicks() {
  const day = document.getElementById("eod-date").value;
  const status = document.getElementById("eod-status");
  if (!day) return;
  try {
    const res = await api(`/eod_tick_csv?date=${encodeURIComponent(day)}`);
    if (!res.ok) { status.textContent = "no tick file on disk for " + day + " (pruned or never recorded)"; return; }
    const name = (res.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)/);
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name ? name[1] : `tsmc_ticks_${day}.csv`;
    a.click();
  } catch (e) {
    status.textContent = "download failed: " + (e.message || e);
  }
}
