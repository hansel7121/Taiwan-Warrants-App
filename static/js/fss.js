// Forced Short Squeeze section (admin): the top-level section switch (Warrant Arbitrage | Forced Short
// Squeeze) and the paper trader's four sub-tabs, all fed by one /fss_state call. /fss_run starts a run
// (services/fss.py); while one is going the section polls until it finishes.

let _fssLoaded = false;
let _fssData = null;
let _fssPoll = null;
const _fssOpenMarks = new Set();

function switchSection(name) {
  document.body.classList.toggle("fss-mode", name === "fss");
  document.getElementById("section-btn-warrant").classList.toggle("active", name !== "fss");
  document.getElementById("section-btn-fss").classList.toggle("active", name === "fss");
  _saveView("ws_section", name);
  if (name === "fss") {
    _fssLoadOnce();
    _fssResizeChart();
  }
}

function restoreSection() {
  if (_readView("ws_section") === "fss") switchSection("fss");
}

function switchFssSub(name, btn) {
  document.querySelectorAll(".fsssub-content").forEach(el => { el.style.display = "none"; });
  document.querySelectorAll(".fsssub-btn").forEach(el => el.classList.remove("active"));
  document.getElementById("fsssub-" + name).style.display = "block";
  btn.classList.add("active");
  if (name === "overview") _fssResizeChart();
}

function _fssResizeChart() {
  const el = document.getElementById("fss-equity");
  if (el && el.data && window.Plotly) Plotly.Plots.resize(el);
}

function _fssEsc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function _fssNum(v, digits) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? "—"
    : Number(v).toLocaleString(undefined, { minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 });
}

function _fssPct(v, digits) {
  if (v === null || v === undefined) return "—";
  const n = Number(v) * 100;
  return `<span class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${n > 0 ? "+" : ""}${n.toFixed(digits === undefined ? 2 : digits)}%</span>`;
}

function _fssTwd(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return `<span class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${n > 0 ? "+" : ""}${_fssNum(n, 0)}</span>`;
}

function _fssWhen(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", { timeZone: "Asia/Taipei", hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function _fssLoadOnce() {
  if (_fssLoaded) return;
  _fssLoaded = true;
  await _fssRefresh();
}

async function _fssRefresh() {
  const status = document.getElementById("fss-status");
  try {
    _fssData = await apiJson("/fss_state");
    _fssRenderStatus();
    _fssRenderOverview();
    _fssRenderPipeline();
    _fssRenderTrades();
    _fssRenderMethod();
    _fssWatch(_fssData.running);
  } catch (e) {
    status.textContent = "load failed: " + (e.message || e);
  }
}

async function _fssRun(kind) {
  const status = document.getElementById("fss-status");
  try {
    const d = await apiJson("/fss_run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind }),
    });
    status.textContent = d.ok ? (kind === "full" ? "Run started…" : "Checking deadlines…") : (d.error || "could not start");
    if (d.ok) _fssWatch(true);
  } catch (e) {
    status.textContent = "run failed: " + (e.message || e);
  }
}

function _fssWatch(running) {
  document.getElementById("fss-run-btn").disabled = !!running;
  document.getElementById("fss-scrape-btn").disabled = !!running;
  if (running && !_fssPoll) {
    _fssPoll = setInterval(async () => {
      try {
        const d = await apiJson("/fss_state");
        if (!d.running) {
          clearInterval(_fssPoll);
          _fssPoll = null;
          await _fssRefresh();
        } else {
          _fssData = d;
          _fssRenderStatus();
        }
      } catch (e) { /* keep polling */ }
    }, 10000);
  }
}

function _fssRenderStatus() {
  const d = _fssData;
  const r = (d.runs || [])[0];
  const lastFull = (d.runs || []).find(x => x.kind === "full" && x.status === "ok");
  let txt = d.running ? "Run in progress… " : "";
  if (r) {
    txt += `Last ${r.kind} run ${_fssWhen(r.run_at)}: ${r.status}`;
    if (r.status === "error") txt += ` — ${r.error || "unknown error"}`;
    if (r.runtime_s !== null && r.runtime_s !== undefined) txt += ` (${_fssNum(r.runtime_s, 0)}s)`;
  } else if (!d.running) {
    txt += "Never run. Click Run now (the first run backfills TWSE data, ~30 min).";
  }
  if (lastFull && lastFull.panel_last) txt += ` · data through ${lastFull.panel_last}`;
  const bad = Object.entries((r && r.sources) || {}).filter(([, v]) => typeof v === "string");
  if (bad.length) txt += ` · source errors: ${bad.map(([k, v]) => `${k} (${v})`).join(", ")}`;
  document.getElementById("fss-status").textContent = txt;
}

function _fssRenderOverview() {
  const d = _fssData;
  const trades = d.trades || [];
  const open = trades.filter(t => t.state !== "closed");
  const closed = trades.filter(t => t.state === "closed");
  const total = trades.reduce((s, t) => s + Number(t.pnl_twd || 0), 0);
  const wins = closed.filter(t => Number(t.net_ret) > 0).length;
  const meanBp = closed.length ? closed.reduce((s, t) => s + Number(t.net_ret), 0) / closed.length * 1e4 : null;
  const upcoming = (d.events || []).filter(e => e.status === "signal" && e.bucket === 5).length;
  const kpi = (lbl, val, foot) => `<div class="fss-kpi"><div class="fss-kpi-lbl">${lbl}</div>
    <div class="fss-kpi-val">${val}</div><div class="fss-kpi-foot">${foot || ""}</div></div>`;
  document.getElementById("fss-kpis").innerHTML = [
    kpi("Paper P&L", _fssTwd(total), `NT$${_fssNum(d.config.notional, 0)} notional per trade`),
    kpi("Open", open.length, `${open.filter(t => t.state === "long").length} long · ${open.filter(t => t.state === "short").length} short`),
    kpi("Closed", closed.length, closed.length ? `hit rate ${(wins / closed.length * 100).toFixed(0)}%` : "—"),
    kpi("Mean net / closed trade", meanBp === null ? "—" : `${meanBp > 0 ? "+" : ""}${meanBp.toFixed(0)}bp`, "after costs"),
    kpi("Q5 cutoff today", d.edges ? d.edges[3].toFixed(3) : "—", `days-to-cover · pool of ${_fssNum(d.pool_n)}`),
    kpi("Q5 awaiting entry", upcoming, "scored on D−17, not yet D−6"),
  ].join("");

  const el = document.getElementById("fss-equity");
  const daily = d.daily || [];
  document.getElementById("fss-equity-empty").style.display = daily.length ? "none" : "";
  el.style.display = daily.length ? "" : "none";
  if (daily.length && window.Plotly) {
    const css = getComputedStyle(document.documentElement);
    const muted = css.getPropertyValue("--muted").trim(), accent = css.getPropertyValue("--accent").trim();
    const grid = css.getPropertyValue("--border").trim(), text = css.getPropertyValue("--text").trim();
    Plotly.react(el, [
      { x: daily.map(r => r.date), y: daily.map(r => r.cum), type: "scatter", mode: "lines", name: "cumulative",
        line: { color: accent, width: 2 }, hovertemplate: "%{x}<br>NT$%{y:,.0f}<extra></extra>" },
      { x: daily.map(r => r.date), y: daily.map(r => r.n_open), type: "bar", name: "open trades", yaxis: "y2",
        marker: { color: muted, opacity: 0.35 }, hovertemplate: "%{y} open<extra></extra>" },
    ], {
      margin: { l: 70, r: 40, t: 10, b: 40 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { color: text, size: 11 }, showlegend: false,
      xaxis: { gridcolor: grid, type: "date" },
      yaxis: { gridcolor: grid, zerolinecolor: muted, tickprefix: "NT$", separatethousands: true },
      yaxis2: { overlaying: "y", side: "right", showgrid: false, rangemode: "tozero", dtick: 1 },
    }, { displayModeBar: false, responsive: true });
  }

  const e = d.edges;
  document.getElementById("fss-edges-note").textContent = e
    ? `Expanding window: every deadline since 2018 whose D has passed (${_fssNum(d.pool_n)} with shorts on D−17). Each deadline is bucketed with the cutoff as of its own D−17.`
    : `Fewer than ${d.config.q_min_hist} past deadlines in the pool — run scripts/fss_seed.py.`;
  document.getElementById("fss-edges").innerHTML = e ? `<thead><tr><th>Bucket</th><th>Days-to-cover</th><th>Traded</th></tr></thead><tbody>
    ${[1, 2, 3, 4, 5].map(q => `<tr><td>Q${q}</td><td>${q === 1 ? `&lt; ${e[0].toFixed(3)}` : q === 5 ? `≥ ${e[3].toFixed(3)}`
      : `${e[q - 2].toFixed(3)} – ${e[q - 1].toFixed(3)}`}</td><td>${q === 5 ? "yes" : ""}</td></tr>`).join("")}</tbody>` : "";

  document.getElementById("fss-open-empty").style.display = open.length ? "none" : "";
  document.getElementById("fss-open-tbody").innerHTML = open.map(t => `<tr>
    <td>${_fssEsc(t.stock_id)}</td><td>${_fssEsc(t.name)}</td><td>${_fssEsc(t.reasons)}</td>
    <td>${t.state === "long" ? '<span class="pos">Long</span>' : '<span class="neg">Short</span>'}</td>
    <td>${t.entry_date}</td><td>${t.d_date}</td><td>${t.exit_date || "—"}</td>
    <td>${_fssNum(t.entry_close, 2)}</td><td>${_fssNum(t.last_close, 2)}</td><td>${_fssNum(t.beta, 2)}</td>
    <td>${_fssNum(t.dtc, 3)}</td><td>${_fssPct(t.net_ret)}</td><td>${_fssTwd(t.pnl_twd)}</td></tr>`).join("");
}

function _fssStatusCell(e) {
  const pill = `<span class="fss-pill ${_fssEsc(e.status)}">${_fssEsc(e.status || "new")}</span>`;
  return pill + (e.note ? `<div class="fss-note">${_fssEsc(e.note)}</div>` : "");
}

function _fssRenderPipeline() {
  if (!_fssData) return;
  const hideSkipped = document.getElementById("fss-hide-skipped").checked;
  const q5 = document.getElementById("fss-q5-only").checked;
  const rows = (_fssData.events || [])
    .filter(e => !(hideSkipped && e.status === "skipped") && !(q5 && e.bucket !== 5))
    .sort((a, b) => (a.d_date > b.d_date ? -1 : a.d_date < b.d_date ? 1 : a.stock_id < b.stock_id ? -1 : 1));
  document.getElementById("fss-pipeline-empty").style.display = rows.length ? "none" : "";
  document.getElementById("fss-pipeline-tbody").innerHTML = rows.map(e => `<tr>
    <td>${_fssEsc(e.stock_id)}</td><td>${_fssEsc(e.name)}</td><td>${_fssEsc((e.reasons || []).join(" / "))}</td>
    <td>${e.d_date}</td><td>${e.sig_date || "—"}</td><td>${e.entry_date || "—"}</td>
    <td>${_fssWhen(e.known_at)}<div class="fss-note">${_fssEsc(Object.keys(e.sources || {}).join(", "))}</div></td>
    <td>${_fssNum(e.short_bal)}</td><td>${_fssNum(e.dtc, 3)}</td>
    <td>${e.bucket ? `<b${e.bucket === 5 ? ' style="color:var(--accent)"' : ""}>Q${e.bucket}</b>` : "—"}</td>
    <td>${_fssNum(e.q80, 3)}</td><td>${e.val20 === null || e.val20 === undefined ? "—" : _fssNum(e.val20 / 1e6, 0) + "m"}</td>
    <td style="min-width:240px;text-align:left">${_fssStatusCell(e)}</td></tr>`).join("");
}

function _fssRenderTrades() {
  const trades = [...(_fssData.trades || [])].sort((a, b) => (a.entry_date < b.entry_date ? 1 : -1));
  document.getElementById("fss-trades-empty").style.display = trades.length ? "none" : "";
  document.getElementById("fss-trades-tbody").innerHTML = trades.map(t => {
    const head = `<tr style="cursor:pointer" onclick="_fssToggleMarks('${_fssEsc(t.id)}')">
      <td>${_fssEsc(t.stock_id)}</td><td>${_fssEsc(t.name)}</td><td>${_fssEsc(t.reasons)}</td><td>${_fssEsc(t.state)}</td>
      <td>${t.entry_date}</td><td>${t.d_date}</td><td>${t.exit_date || "—"}</td>
      <td>${_fssNum(t.beta, 2)}</td><td>${_fssNum(t.dtc, 3)}</td><td>${_fssNum(t.q80, 3)}</td>
      <td>${_fssPct(t.long_ret)}</td><td>${_fssPct(t.short_ret)}</td><td>${_fssPct(t.net_ret)}</td><td>${_fssTwd(t.pnl_twd)}</td></tr>`;
    if (!_fssOpenMarks.has(t.id)) return head;
    const marks = (t.marks || []).map(m => `<tr class="fss-marks"><td></td><td colspan="2">${m.date} (D${m.off >= 0 ? "+" : ""}${m.off})</td>
      <td>${m.side}</td><td colspan="2">close ${_fssNum(m.close, 2)}</td><td colspan="2">stock ${_fssPct(m.stock_ret)}</td>
      <td colspan="2">TAIEX ${_fssPct(m.hedge_ret)}</td><td colspan="2">${m.cost ? `cost −${(m.cost * 1e4).toFixed(1)}bp` : ""}</td>
      <td>${_fssPct(m.pnl)}</td><td>${_fssTwd(m.pnl * (t.notional || _fssData.config.notional))}</td></tr>`).join("");
    return head + (marks || `<tr class="fss-marks"><td colspan="14">No marked day yet — the first is D−5.</td></tr>`);
  }).join("");
}

function _fssToggleMarks(id) {
  if (_fssOpenMarks.has(id)) _fssOpenMarks.delete(id); else _fssOpenMarks.add(id);
  _fssRenderTrades();
}

function _fssRenderMethod() {
  const c = _fssData.config;
  document.getElementById("fss-method").innerHTML = `
    <p>Paper trading of set D in <b>QFS-Pitch-Code / Taiwan Pitch / backtest_noahead.ipynb</b>: the forced short-covering
      trade, entered only on deadlines that were public before the entry.</p>
    <h3>Deadlines</h3>
    <ul>
      <li><b>D</b> = the last day shorts can cover before a short-sale suspension (停券起日). Sources, checked at 08:45, 10:45,
        12:45, 13:15 and with the evening run: TWSE BFI84U (D as listed), TWT48U and MOPS t108sb27 (ex-date − ${4} trading days),
        and the TWSE AGM list t187ap38 (book-closure start − 6 trading days).</li>
      <li><b>Public since</b> = the MOPS posting time if there is one, otherwise the first time the scraper saw the deadline.
        Deadlines of one stock less than 10 trading days apart are one event.</li>
    </ul>
    <h3>Signal (on D−${c.sig_lag})</h3>
    <ul>
      <li>Days-to-cover = short balance ÷ 20-day mean volume (lots). Q5 = at or above the 80th percentile of days-to-cover over
        every deadline whose D fell before this one's D−${c.sig_lag} (expanding window since 2018, deadlines with shorts only;
        none before ${c.q_min_hist} past deadlines). The cutoff moves every day as new deadlines pass.</li>
      <li>20-day mean turnover ≥ NT$${_fssNum(c.min_val20 / 1e6, 0)}m. Ex-dividend / ex-rights and AGM (股東常會) deadlines only.</li>
    </ul>
    <h3>Trade</h3>
    <ul>
      <li>Buy at the close of <b>D−${c.K}</b>, if the deadline was public before 13:25 that day. Flip to short at the close of D,
        cover at the close of <b>D+${c.H}</b>.</li>
      <li>Hedged with −β TAIEX futures while long and +β while short. β = Dimson beta (lags 0–2) on the 250 days ending D−${c.sig_lag},
        Blume-shrunk (0.67β + 0.33).</li>
      <li>Costs ${c.cost_leg_bps}bp + β×${c.hedge_bps}bp on D and on D+${c.H}. Fixed notional NT$${_fssNum(c.notional, 0)} per trade.
        Daily P&amp;L uses dividend-adjusted close-to-close returns, as in the notebook.</li>
      <li>Trades are taken for entries on or after ${c.start} (go-live). Stock returns stand in for single-stock futures on the short leg.</li>
    </ul>`;
}
