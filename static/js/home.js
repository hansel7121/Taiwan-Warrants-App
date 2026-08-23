// Home dashboard: the post-login landing screen — open positions by expiry, the
// realized-P&L curve, live suggestions from the background scanner, a
// Direct-Match launcher, and a "needs attention" queue. Reuses the data and
// helpers already defined in portfolio.js and arb.js rather than re-fetching.

let _homeLoaded = false;

function loadHomeOnce() {
  if (_homeLoaded) { homeResize(); return; }
  _homeLoaded = true;
  loadHome();
}

async function loadHome() {
  // Portfolio + suggestions in parallel; both set the shared module arrays.
  await Promise.all([
    (typeof loadPortfolio === "function" ? loadPortfolio() : Promise.resolve()),
    (typeof loadSuggestions === "function" ? loadSuggestions() : Promise.resolve()),
  ]);
  renderHome();
}

function renderHome() {
  renderHomeKpis();
  renderHomePositions();
  renderHomePnl();
  renderHomeSuggestions();
  renderHomeAttention();
  renderHomeScan();
}

// ── helpers ──────────────────────────────────────────────────────────
const _hnf = (v, d = 0) => Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
const _hsign = (v, d = 0) => (v >= 0 ? "+" : "") + _hnf(v, d);
function _homeDaysLeft(t) { return Math.ceil((_shortExpiryMs(t) - Date.now()) / 86400000); }

function homeResize() {
  try { if (window.Plotly && document.getElementById("home-pnlChart")) Plotly.Plots.resize("home-pnlChart"); } catch (e) {}
}

// Jump to another top-level tab, mirroring the tab-bar onclick wiring.
function goTab(tab) {
  const b = document.querySelector(`.tab-bar button[onclick*="switchTab('${tab}'"]`);
  if (b) switchTab(tab, b);
  if (tab === "portfolio" && typeof loadPortfolioOnce === "function") loadPortfolioOnce();
}
function goSuggestions() {
  goTab("portfolio");
  const b = document.getElementById("pfsub-btn-suggestions");
  if (b && typeof switchPfSub === "function") switchPfSub("suggestions", b);
}

function _homeStats() {
  const pd = (typeof portfolioData !== "undefined" && portfolioData) || [];
  const open = pd.filter(t => !t.closed);
  const closed = pd.filter(t => t.closed);
  const realized = closed.reduce((s, t) => s + (t.closed.realized_pnl || 0), 0);
  let openPnl = 0, openPnlKnown = false;
  open.forEach(t => { const v = _pnlAtExpiryNow(t); if (v != null) { openPnl += v; openPnlKnown = true; } });
  // Capital deployed = cash paid out on the bought (long-warrant) legs of open trades.
  const deployed = open.reduce((s, t) =>
    s + (t.legs || []).reduce((a, l) => a + (l.cf < 0 ? -l.cf : 0), 0), 0);
  const realizingSoon = open.filter(t => _homeDaysLeft(t) <= 2).length;
  return { open, closed, realized, openPnl, openPnlKnown, deployed, realizingSoon };
}

// ── KPI row ──────────────────────────────────────────────────────────
function renderHomeKpis() {
  const s = _homeStats();
  const sug = (typeof suggestionsData !== "undefined" && suggestionsData) || [];
  const kpi = (cls, lbl, val, valCls, foot) =>
    `<div class="home-kpi ${cls}"><div class="home-kpi-lbl">${lbl}</div>
       <div class="home-kpi-val ${valCls}">${val}</div>
       <div class="home-kpi-foot">${foot}</div></div>`;
  const realCls = s.realized >= 0 ? "g" : "";
  const soonFoot = s.realizingSoon
    ? `<span class="warn">${s.realizingSoon} realizes ≤ 2d</span>`
    : "all &gt; 2d out";
  document.getElementById("home-kpis").innerHTML =
    kpi(realCls, "Realized P&L", _hsign(s.realized), s.realized >= 0 ? "pos" : "neg",
        `NT$ · ${s.closed.length} closed trade${s.closed.length === 1 ? "" : "s"}`) +
    kpi("", "Open P&L at expiry", s.openPnlKnown ? _hsign(s.openPnl) : "—",
        s.openPnlKnown ? (s.openPnl >= 0 ? "pos" : "neg") : "", "NT$ · marked at spot now") +
    kpi("", "Open positions", s.open.length, "", soonFoot) +
    kpi("", "Capital deployed", s.deployed >= 1e6 ? _hnf(s.deployed / 1e6, 2) + "M" : _hnf(s.deployed),
        "", "NT$ · long-warrant legs") +
    kpi(sug.length ? "g" : "", "Logged arb suggestions", sug.length, sug.length ? "pos" : "",
        sug.length ? "Direct Match · background scan" : "none logged yet");
}

// ── Open positions, soonest expiry first ─────────────────────────────
function renderHomePositions() {
  const c = document.getElementById("home-positions");
  const s = _homeStats();
  if (!s.open.length) {
    c.innerHTML = `<p class="home-empty">No open positions. Enter one from Arb Finder or a suggestion.</p>`;
    return;
  }
  const rows = s.open
    .map(t => ({ t, d: _homeDaysLeft(t) }))
    .sort((a, b) => a.d - b.d);
  let h = `<table class="home-tbl"><thead><tr>
    <th>Position (long warrant / short option)</th>
    <th class="r">DTE</th><th class="r">張</th><th class="r">Fill</th>
    <th class="r">P&L now</th><th class="r">Status</th></tr></thead><tbody>`;
  rows.forEach(({ t, d }) => {
    const r = t.row || {};
    const typ = r.warrant_type || r.type || "";
    const tag = typ === "Put" ? "put" : "call";
    const now = _pnlAtExpiryNow(t);
    const nowCell = now == null ? `<span class="home-muted">…</span>`
      : `<span class="${now >= 0 ? "pos" : "neg"}">${_hsign(now)}</span>`;
    const dteCls = d <= 2 ? ' style="color:var(--warn)"' : "";
    let chip;
    if (d <= 2) chip = `<span class="home-chip soon">Realizes ${d < 0 ? "now" : d + "d"}</span>`;
    else chip = `<span class="home-chip ok">Open</span>`;
    const fill = r.fillable == null ? "" : r.fillable
      ? `<span class="home-fill-ok">✓</span>` : `<span class="home-fill-no">✗</span>`;
    // Stored trade rows (via openDirectModal) carry warrants_needed, not board_lots;
    // scale by the trade multiplier. 1 張 = 1,000 warrant units.
    const mult = t.mult || 1;
    const lots = r.board_lots != null ? _hnf(r.board_lots * mult, 1)
      : r.warrants_needed ? _hnf(r.warrants_needed * mult / 1000, 1) : "—";
    const name = r.warrant_name || r.warrant_code || t.title || "—";
    const sub = `${r.warrant_strike != null ? "K" + _hnf(r.warrant_strike, 0) : ""}${r.option_contract ? " · short " + r.option_contract : (r.opt_strike != null ? " · short K" + _hnf(r.opt_strike, 0) : "")}`;
    h += `<tr onclick="openPortfolioDetail('${t.id}')" title="Click for the full breakdown">
      <td><span class="home-tag ${tag}">${typ}</span> <span class="home-name">${name}</span>
          <div class="home-sub2">${sub}</div></td>
      <td class="r home-dte"${dteCls}>${d < 0 ? "0" : d}d</td>
      <td class="r">${lots}</td>
      <td class="r">${fill}</td>
      <td class="r">${nowCell}</td>
      <td class="r">${chip}</td></tr>`;
  });
  c.innerHTML = h + "</tbody></table>";
}

// ── Realized-P&L equity curve (Plotly, same math as portfolio.js) ────
function renderHomePnl() {
  const chart = document.getElementById("home-pnlChart");
  const empty = document.getElementById("home-pnlEmpty");
  const foot = document.getElementById("home-pnl-foot");
  const pd = (typeof portfolioData !== "undefined" && portfolioData) || [];
  const closed = pd.filter(t => t.closed)
    .sort((a, b) => new Date(a.closed.close_date) - new Date(b.closed.close_date));
  if (!closed.length || !window.Plotly) {
    empty.style.display = "block"; chart.style.display = "none"; foot.innerHTML = "";
    if (window.Plotly) Plotly.purge(chart);
    return;
  }
  empty.style.display = "none"; chart.style.display = "block";
  let cum = 0, best = -Infinity, wins = 0;
  const x = [], y = [], text = [];
  closed.forEach(t => {
    const p = t.closed.realized_pnl || 0;
    cum += p; best = Math.max(best, p); if (p >= 0) wins++;
    x.push(new Date(t.closed.close_date)); y.push(cum);
    text.push(`${t.title || t.mode}<br>trade P&L: ${_hsign(p)} NT$`);
  });
  const css = getComputedStyle(document.documentElement);
  const pos = css.getPropertyValue("--put").trim() || "#4ade80";
  const muted = css.getPropertyValue("--muted").trim() || "#7b8794";
  Plotly.react(chart, [{
    x, y, text, mode: "lines+markers", type: "scatter", fill: "tozeroy",
    fillcolor: "rgba(74,222,128,0.10)",
    line: { color: pos, width: 2, shape: "hv" }, marker: { size: 5, color: pos },
    hovertemplate: "%{x|%Y-%m-%d}<br>cumulative: %{y:,.0f} NT$<br>%{text}<extra></extra>",
  }], {
    margin: { l: 56, r: 14, t: 8, b: 28 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: muted, size: 10 },
    xaxis: { gridcolor: "rgba(128,128,128,0.12)", zeroline: false },
    yaxis: { gridcolor: "rgba(128,128,128,0.12)", zerolinecolor: "rgba(128,128,128,0.35)", tickformat: "," },
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0, line: { color: muted, width: 1, dash: "dot" } }],
  }, { displayModeBar: false, responsive: true });
  foot.innerHTML =
    `<span>Total <b class="${cum >= 0 ? "pos" : "neg"}">${_hsign(cum)} NT$</b></span>
     <span>Best trade <b>${_hsign(best)}</b></span>
     <span>Win rate <b>${wins} / ${closed.length}</b></span>`;
}

// ── Live risk-free suggestions (top of the background-scanner output) ─
function renderHomeSuggestions() {
  const c = document.getElementById("home-suggestions");
  // Direct Match rows only: this widget renders a warrant/option pair and opens
  // openDirectModal, neither of which fits the LP's variable leg set. The LP's
  // output lives in its own Suggestions sub-tab.
  const all = ((typeof suggestionsData !== "undefined" && suggestionsData) || [])
    .filter(s => s.arb_type !== "static_lp");
  const sug = all.slice(0, 6);
  const label = (typeof _ARB_TYPE_LABEL !== "undefined" && _ARB_TYPE_LABEL) || {};
  if (!sug.length) {
    c.innerHTML = `<p class="home-empty">No arb suggestions logged yet. The scanner appends every Direct Match arb it finds (every 15 min during TWSE hours).</p>`;
    return;
  }
  c.innerHTML = sug.map((s, i) => {
    const r = s.legs || {};
    const pct = s.price_diff_pct, pd = s.price_diff;
    const strikes = r.warrant_strike != null && r.opt_strike != null
      ? `K${_hnf(r.warrant_strike, 0)}→${_hnf(r.opt_strike, 0)}` : "";
    const dte = r.opt_dte != null ? ` · ${r.opt_dte}d` : "";
    const lots = r.board_lots != null ? ` · ${_hnf(r.board_lots, 1)} 張` : "";
    // Direction-aware: rows now include both "Buy Warrant / Sell Option" and
    // the reversed "Buy Option / Sell Warrant", so label each leg by the trade.
    const bw = String(r.trade || "").startsWith("Buy Warrant");
    const gi = suggestionsData.indexOf(s);
    return `<div class="home-srow" onclick="openDirectModal(suggestionsData[${gi}].legs)" title="Click for the full breakdown">
      <div class="home-leg">
        <div class="home-leg-t"><span class="${bw ? "pos" : "neg"}">${bw ? "Buy" : "Sell"}</span> ${r.warrant_name || r.warrant_code || ""} <span class="home-muted">/</span> <span class="${bw ? "neg" : "pos"}">${bw ? "Sell" : "Buy"}</span> ${r.option_contract || ""}</div>
        <div class="home-leg-l">${label[s.arb_type] || s.arb_type} · ${strikes}${dte}${lots}</div>
      </div>
      <div class="home-edge">
        <div class="home-e">${pct == null ? "" : _hsign(pct, 2) + "%"}</div>
        <div class="home-p">${pd == null ? "" : _hsign(pd, 2)}</div>
      </div></div>`;
  }).join("");
}

// ── Needs-attention queue ────────────────────────────────────────────
function renderHomeAttention() {
  const c = document.getElementById("home-attention");
  const s = _homeStats();
  const sug = (typeof suggestionsData !== "undefined" && suggestionsData) || [];
  const items = [];
  s.open.filter(t => _homeDaysLeft(t) <= 2).forEach(t => {
    const d = _homeDaysLeft(t), r = t.row || {};
    items.push({ cls: "warn", ic: "⏳",
      title: `${r.warrant_name || t.title || "Position"} realizes in ${d < 0 ? "0" : d} day${d === 1 ? "" : "s"}.`,
      msg: "Short leg expires — trade auto-closes and books P&L." });
  });
  s.open.filter(t => (t.row || {}).fillable === false).forEach(t => {
    const r = t.row || {};
    items.push({ cls: "neg", ic: "◧",
      title: `${r.warrant_name || t.title || "Position"} — hedge not fully fillable.`,
      msg: `Warrant leg didn't fill at quote; residual directional exposure left open.` });
  });
  if (sug.length) items.push({ cls: "info", ic: "✦",
    title: `${sug.length} arb suggestion${sug.length === 1 ? "" : "s"} logged.`,
    msg: sug[0].price_diff_pct != null ? `Newest edge ${_hsign(sug[0].price_diff_pct, 2)}% — click through to review.` : "Click through to review." });
  if (!items.length) {
    c.innerHTML = `<p class="home-empty">Nothing needs attention. 🎉</p>`;
    return;
  }
  c.innerHTML = items.map(it =>
    `<div class="home-arow ${it.cls}"><div class="home-ic">${it.ic}</div>
       <div class="home-atxt"><b>${it.title}</b><div class="home-m">${it.msg}</div></div></div>`).join("");
}

// ── Direct Match: run the same scan the Arb Finder does, inline ──────
// Reuses the /match_warrant_tw_option route and arb.js's renderCompactArbTable,
// so a row here behaves exactly like one on the Arb Finder tab (click opens the
// Direct Match modal). Rows land in the scrollable results cell below.
let homeArbData = [];

function toggleHomeStock(el) { el.classList.toggle("on"); }

function _homeSelectedStocks() {
  return Array.from(document.querySelectorAll("#home-stocks .home-schip.on")).map(e => e.dataset.code);
}

async function runHomeScan() {
  const status = document.getElementById("home-arb-status");
  const results = document.getElementById("home-arb-results");
  const btn = document.getElementById("home-scan-btn");
  const codes = _homeSelectedStocks();
  if (!codes.length) { status.textContent = "Pick at least one stock."; return; }
  status.textContent = "Scanning " + codes.length + " stock" + (codes.length === 1 ? "" : "s") + "…";
  results.innerHTML = `<p class="home-empty">Fetching warrants and options…</p>`;
  btn.disabled = true;
  let data;
  try {
    const res = await api("/match_warrant_tw_option", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stock_codes: codes, option_type: "All",
        max_strike_diff_pct: parseFloat(document.getElementById("home-arb-strike").value) || 3,
        max_dte_diff: parseInt(document.getElementById("home-arb-dte").value) || 5,
        min_volume: 0, positive_loose: false, strategy: "same_type",
      }),
    });
    data = await res.json();
  } catch (e) {
    btn.disabled = false; status.textContent = "Error: " + e.message;
    results.innerHTML = `<p class="home-empty">Scan failed. Try again.</p>`;
    return;
  }
  btn.disabled = false;
  if (data.error) {
    status.textContent = "";
    results.innerHTML = `<p class="home-empty">No matches: ${data.error}</p>`;
    return;
  }
  homeArbData = data.rows || [];
  const asOf = data.as_of ? " · as of " + new Date(data.as_of).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "";
  status.textContent = `${homeArbData.length} matched pair${homeArbData.length === 1 ? "" : "s"}${asOf}`;
  if (!homeArbData.length) {
    results.innerHTML = `<p class="home-empty">No matches. Try widening the strike / DTE gaps.</p>`;
    return;
  }
  // Same compact table the Arb Finder renders; rows open openDirectModal(homeArbData[i]).
  renderCompactArbTable("home-arb-results", homeArbData, "direct", "homeArbData");
}

// ── Scanner / market freshness line ──────────────────────────────────
function renderHomeScan() {
  const el = document.getElementById("home-scan");
  if (!el) return;
  const twOpen = (document.getElementById("mc-tw-st")?.textContent || "").trim() === "Open";
  const sug = (typeof suggestionsData !== "undefined" && suggestionsData) || [];
  let last = null;
  sug.forEach(s => { const t = s.last_seen_at && new Date(s.last_seen_at); if (t && (!last || t > last)) last = t; });
  const lastStr = last ? last.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "—";
  el.innerHTML = twOpen
    ? `<span class="home-dot"></span>Scanner live · TWSE open · last scan ${lastStr}`
    : `<span class="home-dot idle"></span>Scanner idle · TWSE closed · last scan ${lastStr}`;
}
