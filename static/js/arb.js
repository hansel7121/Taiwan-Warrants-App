// Arb Finder (Direct / US / TW-US), ADR-premium panels, and the PCP trade modal.

async function downloadArbCSV() {
  const res = await api("/match_warrant_tw_option_csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getArbFilters()),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "arb_finder.csv"; a.click();
}

// ── US Option Match ────────────────────────────────────────────────

let currentUsData = [];

let usSortCol = null;

let usSortAsc = true;

let currentTwUsData = [];

let twusSortCol = null;

let twusSortAsc = true;

function openUsInfo() {
  document.getElementById("usInfoModal").style.display = "block";
}

function getUsFilters() {
  const sel = document.getElementById("usStockSelect");
  return {
    stock_codes: Array.from(sel.selectedOptions).map(o => o.value),
    option_type: document.getElementById("usOptionType").value,
    max_strike_diff_pct: parseFloat(document.getElementById("usMaxStrikePct").value) || 3,
    max_dte_diff: parseInt(document.getElementById("usMaxDteDiff").value) ?? 5,
    min_volume: parseInt(document.getElementById("usMinVolume").value) || 0,
    positive_loose: document.getElementById("usPositiveLoose").checked,
    strategy: document.getElementById("usStrategy").value,
  };
}

// Hint only — loose still applies (to the PCP executable leg) for both.

function onUsStrategyChange() {
  const pcp = document.getElementById("usStrategy").value === "pcp";
  document.getElementById("us-status").textContent = pcp
    ? "Cross-market PCP: warrant vs synthetic (opposite-type US option + US ADR + USD bond @ R_US). Executable = long warrant / short synthetic."
    : "";
}

async function matchWarrantUsOption() {
  document.getElementById("us-status").textContent = "Fetching warrants and UMC options…";
  document.getElementById("us-tableContainer").innerHTML = "";
  document.getElementById("usDownloadBtn").style.display = "none";
  let data;
  try {
    const res = await api("/match_warrant_us_option", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getUsFilters()),
    });
    data = await res.json();
  } catch(e) {
    document.getElementById("us-status").textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    document.getElementById("us-status").textContent = "Error: " + data.error;
    return;
  }
  currentUsData = data.rows;
  const usPcp = document.getElementById("usStrategy").value === "pcp";
  document.getElementById("us-status").textContent = usPcp
    ? `${data.count} cross-market PCP pairs — executable: long warrant / short synthetic (short US opp-type option + short/long ADR + USD bond); non-executable (debug): short warrant. Prices TWD/TW-share; 1 US contract = 500 TW shares; FX held constant`
    : `${data.count} matched pairs — prices in TWD/Taiwan-share; 1 US contract = 500 TW shares; FX held constant`;
  document.getElementById("usDownloadBtn").style.display = "inline-block";
  renderUsTable(currentUsData);
}

// IV is no longer computed for arb — hide those columns everywhere.

function renderUsTable(rows) {
  renderCompactArbTable("us-tableContainer", rows, "us", "currentUsData");
}

function sortUsBy(key, asc) {
  if (!currentUsData.length) return;
  currentUsData.sort((a, b) => {
    const av = a[key] ?? (asc ? Infinity : -Infinity);
    const bv = b[key] ?? (asc ? Infinity : -Infinity);
    return asc ? av - bv : bv - av;
  });
  renderUsTable(currentUsData);
}

function openUsModal(row) {
  // Cross-market PCP rows carry the `executable` flag (opposite-type
  // option + real synthetic/bond); same-type rows do not.
  const isPcp = "executable" in row;
  const mapped = {
    warrant_code:     row.warrant_code,
    warrant_name:     row.warrant_name,
    warrant_type:     row.type,
    opt_type:         isPcp ? row.opt_type : row.type,  // PCP pairs opposite type
    option_contract:  row.option_contract,
    underlying_price: row.underlying_price,   // warrant LOCAL spot (premium baseline)
    adr_underlying:   row.adr_underlying,     // ADR-converted spot (synthetic/stock leg)
    warrant_dte:      row.warrant_dte,
    opt_dte:          row.opt_dte,
    dte_diff:         row.dte_diff,
    warrant_strike:   row.warrant_strike,
    opt_strike:       row.opt_strike,
    strike_diff_pct:  row.strike_diff_pct,
    warrants_needed:  row.warrants_needed,
    opt_contract_size:row.opt_contract_size,
    warrant_depth_lots: row.warrant_depth_lots,
    fillable:         row.fillable,
    warrant_ask:      row.warrant_ask,
    warrant_bid:      row.warrant_bid,
    opt_bid:          row.opt_bid,
    opt_ask:          row.opt_ask,
    warrant_per_share:row.warrant_per_share,
    opt_per_share:    row.opt_per_share,
    pcp_diff:         row.price_diff,
    pcp_diff_pct:     row.price_diff_pct,
    // PCP: real synthetic + USD bond PV from the backend. Same-type:
    // synthetic = the option price itself, no bond leg.
    synthetic_price:  isPcp ? row.synthetic_price : row.opt_per_share,
    bond_pv:          isPcp ? row.bond_pv : null,
    executable:       isPcp ? row.executable : true,
    warrant_iv:       row.warrant_iv,
    opt_iv:           row.opt_iv,
    us_stock_code:    row.us_stock_code,
    loose:            document.getElementById("usPositiveLoose").checked,
  };
  openArbModal(mapped, isPcp ? "uspcp" : "us");
}

async function downloadUsCSV() {
  const res = await api("/match_warrant_us_option_csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getUsFilters()),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "us_option_match.csv"; a.click();
}

// ── TW Option / US Option sub-tab ──────────────────────────────────

function getTwUsFilters() {
  const sel = document.getElementById("twusStockSelect");
  return {
    stock_codes: Array.from(sel.selectedOptions).map(o => o.value),
    option_type: document.getElementById("twusOptionType").value,
    max_strike_diff_pct: parseFloat(document.getElementById("twusMaxStrikePct").value) || 2,
    max_dte_diff: parseInt(document.getElementById("twusMaxDteDiff").value) ?? 10,
    min_volume: parseInt(document.getElementById("twusMinVolume").value) || 0,
  };
}

async function matchTwUsOption() {
  document.getElementById("twus-status").textContent = "Fetching Taiwan + US options…";
  document.getElementById("twus-tableContainer").innerHTML = "";
  document.getElementById("twusDownloadBtn").style.display = "none";
  let data;
  try {
    const res = await api("/match_tw_us_option", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getTwUsFilters()),
    });
    data = await res.json();
  } catch(e) {
    document.getElementById("twus-status").textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    document.getElementById("twus-status").textContent = "Error: " + data.error;
    return;
  }
  currentTwUsData = data.rows;
  document.getElementById("twus-status").textContent =
    `${data.count} matched pairs — entry credit = sell the richer leg / buy the cheaper. Click a row for the ADR-premium scenario P&L.`;
  document.getElementById("twusDownloadBtn").style.display = "inline-block";
  renderTwUsTable(currentTwUsData);
}

function renderTwUsTable(rows) {
  renderCompactArbTable("twus-tableContainer", rows, "twus", "currentTwUsData");
}

function sortTwUsBy(key, asc) {
  if (!currentTwUsData.length) return;
  currentTwUsData.sort((a, b) => {
    const av = a[key] ?? (asc ? Infinity : -Infinity);
    const bv = b[key] ?? (asc ? Infinity : -Infinity);
    return asc ? av - bv : bv - av;
  });
  renderTwUsTable(currentTwUsData);
}

function openTwUsModal(row) {
  // TW option → warrant_* slots, US option → opt_* slots (already emitted so).
  const mapped = {
    warrant_code:     row.tw_option_code,
    warrant_name:     row.tw_option_name,
    warrant_type:     row.type,
    opt_type:         row.type,
    option_contract:  row.us_option_contract,
    underlying_price: row.underlying_price,
    warrant_dte:      row.tw_option_dte,
    opt_dte:          row.us_option_dte,
    dte_diff:         row.dte_diff,
    warrant_strike:   row.tw_option_strike,
    opt_strike:       row.us_option_strike,
    strike_diff_pct:  row.strike_diff_pct,
    warrants_needed:  row.tw_contracts_needed,
    opt_contract_size:row.us_option_contract_size,
    tw_contracts:     row.tw_contracts,
    us_contracts:     row.us_contracts,
    tw_depth_contracts: row.tw_depth_contracts,
    tw_fillable:      row.tw_fillable,
    us_volume:        row.us_volume,
    us_oi:            row.us_oi,
    warrant_ask:      row.tw_option_ask,
    warrant_bid:      row.tw_option_bid,
    opt_bid:          row.us_option_bid,
    opt_ask:          row.us_option_ask,
    warrant_per_share:row.tw_option_per_share,
    opt_per_share:    row.us_option_per_share,
    pcp_diff:         row.price_diff,
    pcp_diff_pct:     row.price_diff_pct,
    synthetic_price:  row.us_option_per_share,
    bond_pv:          null,
    warrant_iv:       row.tw_option_iv,
    opt_iv:           row.us_option_iv,
    us_stock_code:    row.us_stock_code,
    loose:            false,   // TW/US tab has no loose toggle (always executable)
  };
  openArbModal(mapped, "twus");
}

async function downloadTwUsCSV() {
  const res = await api("/match_tw_us_option_csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getTwUsFilters()),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "tw_us_option_match.csv"; a.click();
}

function openTwUsInfo() { openUsInfo(); }

// ── ADR premium risk + Expected Value (US mode) ────────────────────
//

async function loadAdrPremium(row) {
  const setTxt = (id, t) => { document.getElementById(id).textContent = t; };
  setTxt("us-latest-prem", "…");
  document.getElementById("us-prem-flag").textContent = "";
  document.getElementById("us-prem-adf").innerHTML = "";
  Plotly.purge("us-prem-chart");
  Plotly.purge("us-prem-hist");

  let s;
  try {
    const res = await api("/adr_premium", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stock_code: row.us_stock_code }),
    });
    s = await res.json();
  } catch (e) { setTxt("us-latest-prem", "error"); return; }
  if (s.error) { setTxt("us-latest-prem", "n/a"); return; }

  // Latest premium badge
  const p0 = s.latest_premium;                 // fraction
  const p0pct = p0 * 100;
  const el = document.getElementById("us-latest-prem");
  el.textContent = (p0pct >= 0 ? "+" : "") + p0pct.toFixed(2) + "%";
  const near0 = Math.abs(p0pct) <= 1;
  el.style.color = near0 ? "var(--put)" : Math.abs(p0pct) > 5 ? "var(--call)" : "var(--text)";
  const flag = document.getElementById("us-prem-flag");
  flag.textContent = near0 ? "GOOD ENTRY (≈0)" : Math.abs(p0pct) > 5 ? "DISLOCATED — risky" : "off-zero — caution";
  flag.style.background = near0 ? "rgba(74,222,128,0.15)" : Math.abs(p0pct) > 5 ? "rgba(248,113,113,0.15)" : "rgba(251,191,36,0.15)";
  flag.style.color = near0 ? "var(--put)" : Math.abs(p0pct) > 5 ? "var(--call)" : "#fbbf24";

  // Premium history chart with ±threshold bands
  const thr = s.threshold * 100;
  Plotly.react("us-prem-chart", [
    { x: s.dates, y: s.premium_pct, mode: "lines", name: "ADR premium",
      line: { color: "var(--accent)", width: 1.3 },
      hovertemplate: "%{x}<br>premium: %{y:+.2f}%<extra></extra>" },
    { x: [s.dates[0], s.dates[s.dates.length-1]], y: [thr, thr], mode: "lines",
      line: { color: "rgba(248,113,113,0.5)", width: 1, dash: "dash" }, showlegend: false, hoverinfo: "skip" },
    { x: [s.dates[0], s.dates[s.dates.length-1]], y: [-thr, -thr], mode: "lines",
      line: { color: "rgba(248,113,113,0.5)", width: 1, dash: "dash" }, showlegend: false, hoverinfo: "skip" },
    { x: [s.dates[0], s.dates[s.dates.length-1]], y: [0, 0], mode: "lines",
      line: { color: "rgba(255,255,255,0.15)", width: 1 }, showlegend: false, hoverinfo: "skip" },
  ], {
    paper_bgcolor: "#13161f", plot_bgcolor: "#0d0f16",
    font: { color: "#8b90a0", family: "Inter,system-ui,sans-serif", size: 11 },
    xaxis: { gridcolor: "#252836", zerolinecolor: "#252836" },
    yaxis: { title: "premium %", gridcolor: "#252836", zerolinecolor: "#252836", ticksuffix: "%" },
    margin: { l: 55, r: 10, t: 10, b: 30 }, showlegend: false,
  }, { responsive: true });

  // ADF unit-root test: proves (or disproves) the premium is mean-reverting.
  // Null = unit root (random walk); p < 0.05 rejects it => mean-reverting,
  // which is the trade's whole thesis. Half-life = trading days to revert halfway.
  const adfEl = document.getElementById("us-prem-adf");
  const a = s.adf;
  if (!a || a.pvalue == null) {
    const why = a && a.reason ? " (" + a.reason + ")" : "";
    adfEl.innerHTML = `<span style="color:var(--muted)">ADF test: not enough data${why}.</span>`;
  } else {
    const ok = a.stationary;
    const col = ok ? "var(--put)" : "var(--call)";
    const bg = ok ? "rgba(74,222,128,0.15)" : "rgba(248,113,113,0.15)";
    const verdict = ok ? "MEAN-REVERTING ✓" : "UNIT ROOT — not mean-reverting ✗";
    const pStr = a.pvalue < 0.001 ? "&lt;0.001" : a.pvalue.toFixed(4);
    const hl = a.half_life != null
      ? ` Half-life ≈ <b>${a.half_life < 1 ? a.half_life.toFixed(1) : Math.round(a.half_life)}</b> trading days.`
      : "";
    const c5 = a.crit && a.crit["5%"] != null ? a.crit["5%"].toFixed(2) : "—";
    adfEl.innerHTML =
      `<span style="font-weight:700;font-size:11px;letter-spacing:.3px;padding:2px 8px;border-radius:10px;color:${col};background:${bg}">ADF: ${verdict}</span>`
      + `<span style="color:var(--muted);margin-left:10px">p = <b style="color:var(--text)">${pStr}</b>`
      + ` · stat ${a.stat.toFixed(2)} vs 5% crit ${c5}`
      + ` · n=${a.nobs}.${hl}</span>`
      + `<div style="color:var(--muted);margin-top:5px">ADF null = random walk (no reversion). ${ok
          ? `p &lt; 0.05 rejects it: the premium pulls back to its mean, so a dislocation is expected to revert — the basis edge is real.`
          : `p ≥ 0.05 can't reject a random walk: no statistical mean-reversion, so don't count on the premium snapping back.`}</div>`;
  }

  // Distribution (histogram) of daily premiums, with ±threshold lines
  // and their historical tail probabilities.
  const pHi = s.regimes[0].prob * 100;   // P(premium > +thr)
  const pLo = s.regimes[2].prob * 100;   // P(premium < -thr)
  const vline = (x, color) => ({
    type: "line", x0: x, x1: x, yref: "paper", y0: 0, y1: 1,
    line: { color, width: 1.5, dash: "dash" },
  });
  Plotly.react("us-prem-hist", [
    { x: s.premium_pct, type: "histogram", nbinsx: 60,
      marker: { color: "rgba(96,165,250,0.55)", line: { color: "rgba(96,165,250,0.9)", width: 0.5 } },
      hovertemplate: "premium %{x:.2f}%<br>count %{y}<extra></extra>" },
  ], {
    paper_bgcolor: "#13161f", plot_bgcolor: "#0d0f16",
    font: { color: "#8b90a0", family: "Inter,system-ui,sans-serif", size: 11 },
    xaxis: { title: "daily ADR premium %", gridcolor: "#252836", zerolinecolor: "#252836", ticksuffix: "%" },
    yaxis: { title: "days", gridcolor: "#252836", zerolinecolor: "#252836" },
    margin: { l: 55, r: 10, t: 24, b: 38 }, showlegend: false, bargap: 0.02,
    shapes: [ vline(thr, "rgba(248,113,113,0.8)"), vline(-thr, "rgba(248,113,113,0.8)"), vline(0, "rgba(255,255,255,0.25)") ],
    annotations: [
      { x: thr, xanchor: "left", yref: "paper", y: 1, yanchor: "top", text: `P(> +${thr}%) = ${pHi.toFixed(1)}%`,
        showarrow: false, font: { color: "#f87171", size: 11 }, bgcolor: "rgba(19,22,31,0.8)", xshift: 4 },
      { x: -thr, xanchor: "right", yref: "paper", y: 1, yanchor: "top", text: `P(< -${thr}%) = ${pLo.toFixed(1)}%`,
        showarrow: false, font: { color: "#f87171", size: 11 }, bgcolor: "rgba(19,22,31,0.8)", xshift: -4 },
    ],
  }, { responsive: true });

  // (Unconditional EV table removed — the conditional "enter now" scenario
  //  below is now the single EV, for both US Option Match and TW/US Opt.)
}

// Conditional "enter now" scenario: given today's premium, where does it
// land by the first expiry, and what is the P&L in each case?

async function loadAdrScenario(row) {
  const tbl = document.getElementById("us-scenario-table");
  const cap = document.getElementById("us-scenario-caption");
  const note = document.getElementById("us-scenario-note");
  tbl.innerHTML = ""; note.innerHTML = ""; cap.textContent = "loading…";
  document.getElementById("us-scenario-breakeven").innerHTML = "";
  document.getElementById("us-fx-section").style.display = "none";

  // Trade closes at the first (nearest) expiry.
  const horizon = Math.max(1, Math.min(row.warrant_dte, row.opt_dte));
  let s;
  try {
    const res = await api("/adr_premium_scenario", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stock_code: row.us_stock_code, horizon_days: horizon }),
    });
    s = await res.json();
  } catch (e) { cap.textContent = "scenario error"; return; }
  if (s.error) { cap.textContent = s.error; return; }

  const p0 = s.current_premium;
  // Snapshot entry premium/FX for the Portfolio "Enter Trade" button.
  _pcpEntry = { premium: s.current_premium, fx: s.fx ? s.fx.current_fx : null };
  cap.innerHTML =
    `Premium is <b>${(p0*100>=0?"+":"")}${(p0*100).toFixed(2)}%</b> right now. Centering on it and applying every historical ${s.horizon_days}d (${s.horizon_trading} trading-day) premium move (${s.n_samples} windows), the exit premium lands:`;

  const th = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border)";
  const td = "padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text)";
  const tdr = td + ";text-align:right;font-variant-numeric:tabular-nums";
  let ev = 0;
  let body = `<thead><tr>
    <th style="${th};text-align:left">Premium by expiry</th>
    <th style="${th};text-align:right">P (from here)</th>
    <th style="${th};text-align:right">Exit premium</th>
    <th style="${th};text-align:right">Trade P&L</th>
    <th style="${th};text-align:right">P × P&L</th>
  </tr></thead><tbody>`;
  s.regimes.forEach(r => {
    // E[PnL | band] = average P&L over EACH conditioned window in this band,
    // pricing the option leg's moneyness off the window's FX-STRIPPED
    // (basis) premium with FX held flat. FX is priced separately below as
    // its own EV line, so the two risks never double-count. Nonlinear P&L
    // => average the P&L, not P&L at the band mean.
    const prem = r.premiums && r.premiums.length ? r.premiums : null;
    const pnl = prem
      ? prem.reduce((sum, pp) => sum + _evPnlAtPremium(row, pp, p0, 1), 0) / prem.length
      : _evPnlAtPremium(row, r.cond_premium, p0, 1);
    const contrib = r.prob * pnl;
    ev += contrib;
    const col = pnl >= 0 ? "var(--put)" : "var(--call)";
    body += `<tr>
      <td style="${td}">${r.label}</td>
      <td style="${tdr}">${(r.prob*100).toFixed(1)}%</td>
      <td style="${tdr}">${(r.cond_premium*100>=0?"+":"")}${(r.cond_premium*100).toFixed(2)}%</td>
      <td style="${tdr};color:${col}">${pnl>=0?"+":""}${Math.round(pnl).toLocaleString()}</td>
      <td style="${tdr};color:${col}">${contrib>=0?"+":""}${Math.round(contrib).toLocaleString()}</td>
    </tr>`;
  });
  const evCol = ev >= 0 ? "var(--put)" : "var(--call)";
  body += `</tbody><tfoot><tr>
    <td colspan="4" style="${td};font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.4px;color:var(--muted)">Premium EV — FX-stripped basis (NT$)</td>
    <td style="${tdr};font-weight:700;font-size:14px;color:${evCol}">${ev>=0?"+":""}${Math.round(ev).toLocaleString()}</td>
  </tr></tfoot>`;
  tbl.innerHTML = body;
  _premEvNt = ev;   // hand the premium EV to the FX section for the total

  // Break-even exit premium (FX held flat): scan for the exit premium at
  // which trade P&L crosses zero. Single interpretable threshold next to the
  // regime table — "the trade needs the premium to stay above/below X%".
  const pnlAtP = (pp) => _evPnlAtPremium(row, pp, p0, 1);
  const loP = p0 - 0.6, hiP = p0 + 0.6, steps = 240;
  let be = null, prev = pnlAtP(loP), prevP = loP;
  for (let i = 1; i <= steps; i++) {
    const pp = loP + (hiP - loP) * i / steps, cur = pnlAtP(pp);
    if ((prev <= 0 && cur >= 0) || (prev >= 0 && cur <= 0)) {
      const t = (0 - prev) / ((cur - prev) || 1);
      be = prevP + (pp - prevP) * t; break;
    }
    prev = cur; prevP = pp;
  }
  const pnlNow = pnlAtP(p0);
  const beTxt = be == null
    ? `P&L (FX flat) does not cross zero within ±60% premium of today — it is ${pnlNow >= 0 ? "positive" : "negative"} across that range.`
    : `Break-even exit premium (FX flat): <b>${(be*100>=0?"+":"")}${(be*100).toFixed(2)}%</b> `
      + `(today ${(p0*100>=0?"+":"")}${(p0*100).toFixed(2)}%). Trade is profitable while the premium stays ${pnlNow >= 0 ? (be < p0 ? "above" : "below") : (be < p0 ? "below" : "above")} it.`;
  const beCol = pnlNow >= 0 ? "var(--put)" : "var(--call)";
  document.getElementById("us-scenario-breakeven").innerHTML =
    `<span style="color:${beCol}">${beTxt}</span>`;

  note.innerHTML =
    `Premium EV = Σ <code>P(band)</code> · <code>E[PnL | band]</code> over each window's <b>FX-stripped basis premium</b> (the premium move NOT explained by FX), with FX held flat. FX is priced separately as its own EV line in the panel below, so the two risks add without double-counting. `
    + `Basis moves are the <b>unconditional</b> distribution of ${row.warrant_dte < row.opt_dte ? row.warrant_dte : row.opt_dte} trading-day premium-ex-FX moves, centered on today's premium. `
    + `"Exit premium" is the band mean, reference only. Horizon = min(TW ${row.warrant_dte}d, US ${row.opt_dte}d).`;

  renderFxSection(row, s, p0);
}

// FX (TWD/USD) factor + charts, isolated from the ADR premium.

function renderFxSection(row, s, p0) {
  const sec = document.getElementById("us-fx-section");
  if (!s.fx || !s.fx_series) { sec.style.display = "none"; return; }
  sec.style.display = "block";
  const fx = s.fx, ser = s.fx_series;

  const chg30 = fx.chg_30d_pct ?? 0;
  document.getElementById("us-fx-rate").textContent = fx.current_fx.toFixed(3);
  const fxBadge = document.getElementById("us-fx-badge");
  fxBadge.textContent = `30d ${chg30>=0?"+":""}${chg30.toFixed(2)}%`;
  const up30 = chg30 >= 0;
  fxBadge.style.background = up30 ? "rgba(74,222,128,0.15)" : "rgba(248,113,113,0.15)";
  fxBadge.style.color = up30 ? "var(--put)" : "var(--call)";
  document.getElementById("us-fx-caption").innerHTML =
    `The US leg settles in USD and converts to TWD at exit — that FX risk is the table below (its own EV line). The line below-left is the ADR premium <b>with FX stripped out</b> (compounded basis moves, index=100): the premium drift NOT explained by FX (${(fx.unexplained_frac*100).toFixed(0)}% of premium variance) — the US-only basis risk you carry on top of FX.`;

  // ADR premium with FX stripped out: compound the residual daily basis
  // moves (Δln(1+prem) − Δln(FX)) into an index=100. This is the premium
  // risk that survives after FX; resid_pct[i] corresponds to dates[i+1].
  const isoDates = ser.dates.slice(1);
  const iso = []; let lvl = 100;
  (ser.resid_pct || []).forEach(r => { lvl *= (1 + r / 100); iso.push(lvl); });
  Plotly.react("us-fx-chart", [
    { x: isoDates, y: iso, mode: "lines", line: { color: "#a78bfa", width: 1.3 },
      hovertemplate: "%{x}<br>premium ex-FX index %{y:.2f}<extra></extra>" },
  ], {
    paper_bgcolor: "#13161f", plot_bgcolor: "#0d0f16",
    font: { color: "#8b90a0", family: "Inter,system-ui,sans-serif", size: 11 },
    xaxis: { gridcolor: "#252836", zerolinecolor: "#252836" },
    yaxis: { title: "premium ex-FX (index=100)", gridcolor: "#252836", zerolinecolor: "#252836" },
    margin: { l: 55, r: 10, t: 8, b: 30 }, showlegend: false,
  }, { responsive: true });

  // Daily premium moves with FX stripped out (the US-only basis residual)
  Plotly.react("us-fx-hist", [
    { x: ser.resid_pct, type: "histogram", nbinsx: 60,
      marker: { color: "rgba(167,139,250,0.5)", line: { color: "rgba(167,139,250,0.9)", width: 0.5 } },
      hovertemplate: "premium ex-FX %{x:.2f}%<br>count %{y}<extra></extra>" },
  ], {
    paper_bgcolor: "#13161f", plot_bgcolor: "#0d0f16",
    font: { color: "#8b90a0", family: "Inter,system-ui,sans-serif", size: 11 },
    xaxis: { title: "daily premium move not explained by FX (%)", gridcolor: "#252836", zerolinecolor: "#252836", ticksuffix: "%" },
    yaxis: { title: "days", gridcolor: "#252836", zerolinecolor: "#252836" },
    margin: { l: 55, r: 10, t: 10, b: 38 }, showlegend: false, bargap: 0.02,
  }, { responsive: true });

  // Scatter: daily ADR-premium change vs daily FX change (the correlation).
  if (ser.dprem_pct && ser.dfx_pct) {
    // Least-squares best-fit line ΔFX = a + b·Δprem over the plotted points.
    const xs = ser.dprem_pct, ys = ser.dfx_pct, m = xs.length;
    let sx=0, sy=0, sxx=0, sxy=0;
    for (let i=0;i<m;i++){ sx+=xs[i]; sy+=ys[i]; sxx+=xs[i]*xs[i]; sxy+=xs[i]*ys[i]; }
    const b = (m*sxy - sx*sy) / (m*sxx - sx*sx);
    const a = (sy - b*sx) / m;
    const xmin = Math.min(...xs), xmax = Math.max(...xs);
    Plotly.react("us-fx-scatter", [
      { x: ser.dprem_pct, y: ser.dfx_pct, mode: "markers", type: "scattergl",
        marker: { color: "rgba(167,139,250,0.4)", size: 4 }, name: "days",
        hovertemplate: "Δprem %{x:.2f}%<br>ΔFX %{y:.2f}%<extra></extra>" },
      { x: [xmin, xmax], y: [a+b*xmin, a+b*xmax], mode: "lines", name: "best fit",
        line: { color: "#f0abfc", width: 2 }, hoverinfo: "skip" },
    ], {
      paper_bgcolor: "#13161f", plot_bgcolor: "#0d0f16",
      font: { color: "#8b90a0", family: "Inter,system-ui,sans-serif", size: 11 },
      title: { text: `corr = ${fx.corr_with_premium.toFixed(2)}  ·  R² = ${(fx.r2_with_premium*100).toFixed(0)}%  ·  slope = ${b.toFixed(3)}`, font: { size: 11, color: "#8b90a0" }, x: 0, xanchor: "left" },
      xaxis: { title: "Δ ADR premium (%)", gridcolor: "#252836", zerolinecolor: "#3a3f52", ticksuffix: "%" },
      yaxis: { title: "Δ FX (%)", gridcolor: "#252836", zerolinecolor: "#3a3f52", ticksuffix: "%" },
      margin: { l: 55, r: 10, t: 28, b: 40 }, showlegend: false,
    }, { responsive: true });
  }

  // Which FX direction hurts this trade? Bump FX +1% and see P&L sign.
  const base = _evPnlAtPremium(row, p0, p0, 1);
  const bumped = _evPnlAtPremium(row, p0, p0, 1.01);
  const upHelps = (bumped - base) >= 0;   // TWD weakening (fx>1) helps?
  const againstDir = upHelps ? "down" : "up";   // the harmful move

  const th = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border)";
  const td = "padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text)";
  const tdr = td + ";text-align:right;font-variant-numeric:tabular-nums";
  const rowFx = (label, prob, meanPct, dirTag) => {
    const fxRatio = 1 + meanPct / 100;
    const pnl = _evPnlAtPremium(row, p0, p0, fxRatio);
    const dPnl = pnl - base;
    const against = dirTag && dirTag === againstDir;
    const col = dPnl >= 0 ? "var(--put)" : "var(--call)";
    return `<tr>
      <td style="${td}">${label}${against ? ' <span style="color:var(--call);font-size:10px">◀ against you</span>' : ''}</td>
      <td style="${tdr}">${(prob*100).toFixed(1)}%</td>
      <td style="${tdr}">${meanPct>=0?"+":""}${meanPct.toFixed(2)}%</td>
      <td style="${tdr};color:${col}">${dPnl>=0?"+":""}${Math.round(dPnl).toLocaleString()}</td>
    </tr>`;
  };
  // FX EV = Σ P(regime)·(ΔP&L vs flat FX): the standalone FX contribution,
  // moneyness held flat so it does not overlap the premium EV. Total EV =
  // Premium EV (basis) + FX EV — two clean, non-overlapping numbers.
  const fxEv = [[fx.stay_prob, 0], [fx.up_prob, fx.up_mean_pct], [fx.down_prob, fx.down_mean_pct]]
    .reduce((sum, [prob, mv]) => sum + (prob || 0) * (_evPnlAtPremium(row, p0, p0, 1 + mv / 100) - base), 0);
  const totalEv = _premEvNt + fxEv;
  const fxCol = fxEv >= 0 ? "var(--put)" : "var(--call)";
  const totCol = totalEv >= 0 ? "var(--put)" : "var(--call)";
  document.getElementById("us-fx-table").innerHTML = `
    <thead><tr>
      <th style="${th};text-align:left">FX by expiry (from ${fx.current_fx.toFixed(3)})</th>
      <th style="${th};text-align:right">P (from here)</th>
      <th style="${th};text-align:right">Mean move</th>
      <th style="${th};text-align:right">Δ P&L vs flat FX</th>
    </tr></thead><tbody>
      ${rowFx("Stays (±0.5%)", fx.stay_prob, 0, null)}
      ${rowFx("TWD weakens (FX up)", fx.up_prob, fx.up_mean_pct, "up")}
      ${rowFx("TWD strengthens (FX down)", fx.down_prob, fx.down_mean_pct, "down")}
    </tbody><tfoot>
      <tr><td colspan="3" style="${td};font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.4px;color:var(--muted)">FX EV (NT$)</td>
          <td style="${tdr};font-weight:700;color:${fxCol}">${fxEv>=0?"+":""}${Math.round(fxEv).toLocaleString()}</td></tr>
      <tr><td colspan="3" style="${td};font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.4px;color:var(--muted)">Total EV = Premium + FX (NT$)</td>
          <td style="${tdr};font-weight:700;font-size:14px;color:${totCol}">${totalEv>=0?"+":""}${Math.round(totalEv).toLocaleString()}</td></tr>
    </tfoot>`;

  document.getElementById("us-fx-note").innerHTML =
    `Premium ex-FX uses the exact identity Δln(1+prem) = Δln(FX) + Δbasis (FX enters premium with coefficient 1), so the histogram above is the <b>basis residual</b> — the premium move FX can't explain (${(fx.unexplained_frac*100).toFixed(0)}% of premium variance; corr(premium, FX) = ${fx.corr_with_premium.toFixed(2)}). `
    + `FX-EV probabilities condition on FX starting within ±${fx.cond_band_pct}% of today's rate, ${s.horizon_trading} trading days out (${fx.n} windows). `
    + `Total EV = Premium EV (basis, FX flat) + FX EV (moneyness flat), so neither risk is double-counted.`;
}

// ── Options Scanner ────────────────────────────────────────────────

let currentArbData = [];

let arbSortCol = null;

let arbSortAsc = true;

function setArbMode(mode) {
  const modes = ["direct", "us", "twus", "straddle"];
  modes.forEach(m => {
    const active = m === mode;
    const btn = document.getElementById("arb-mode-" + m);
    btn.classList.toggle("active", active);
    document.getElementById("arb-sub-" + m).style.display = active ? "block" : "none";
  });
}

function matchWarrantTwOption() {
  fetchArbData();
}

function getArbFilters() {
  const sel = document.getElementById("arbStockSelect");
  return {
    stock_codes: Array.from(sel.selectedOptions).map(o => o.value),
    option_type: document.getElementById("arbOptionType").value,
    max_strike_diff_pct: parseFloat(document.getElementById("arbMaxStrikePct").value) || 3,
    max_dte_diff: parseInt(document.getElementById("arbMaxDteDiff").value) ?? 5,
    min_volume: parseInt(document.getElementById("arbMinVolume").value) || 0,
    positive_loose: document.getElementById("arbPositiveLoose").checked,
    strategy: document.getElementById("arbStrategy").value,
  };
}

// Loose applies to both strategies: same-type positive leg, and (PCP) the
// executable long-warrant/short-synthetic leg. Keep it visible everywhere.

function onArbStrategyChange() {
  document.getElementById("arbPositiveLoose").closest("label").style.display = "";
}

async function fetchArbData() {
  document.getElementById("arb-status").textContent = "Fetching warrants and options…";
  document.getElementById("arb-tableContainer").innerHTML = "";
  document.getElementById("arbDownloadBtn").style.display = "none";
  let data;
  try {
    const res = await api("/match_warrant_tw_option", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getArbFilters()),
    });
    data = await res.json();
  } catch(e) {
    document.getElementById("arb-status").textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    document.getElementById("arb-status").textContent = "Error: " + data.error;
    return;
  }
  currentArbData = data.rows;
  const pcp = document.getElementById("arbStrategy").value === "pcp";
  document.getElementById("arb-status").textContent = (pcp
    ? `${data.count} PCP pairs — executable: long warrant / short synthetic; non-executable (debug): short warrant / long synthetic`
    : `${data.count} matched pairs — positive price_diff: buy warrant / sell option; negative: buy option / sell warrant`) + asOfLabel(data);
  document.getElementById("arbDownloadBtn").style.display = "inline-block";
  renderArbTable(currentArbData);
}

// ── Compact arb result table (shared by Direct / US / TW-US) ───────────
// Five decision columns. Everything trimmed — both sides of every quote,
// depth counts, contract sizes, IV — is still reachable: click the row for
// the full modal, or hit Download CSV, which exports every field.
// Each mode names its two legs and where its strike/DTE fields live, since
// TW/US rows carry tw_option_*/us_option_* while Direct/US carry warrant_*/opt_*.
const ARB_TBL = {
  direct: { open: "openDirectModal", legA: "Warrant",   legB: "TW Option",
            aName: "warrant_name", aK: "warrant_strike", aD: "warrant_dte",
            bName: "option_contract", bK: "opt_strike", bD: "opt_dte" },
  us:     { open: "openUsModal",     legA: "Warrant",   legB: "US Option",
            aName: "warrant_name", aK: "warrant_strike", aD: "warrant_dte",
            bName: "option_contract", bK: "opt_strike", bD: "opt_dte" },
  twus:   { open: "openTwUsModal",   legA: "TW Option", legB: "US Option",
            aName: "tw_option_name", aK: "tw_option_strike", aD: "tw_option_dte",
            bName: "us_option_contract", bK: "us_option_strike", bD: "us_option_dte" },
};
const _n = (v, d = 2) => v == null ? "—"
  : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });

function renderCompactArbTable(containerId, rows, mode, arrName) {
  const cfg = ARB_TBL[mode];
  const c = document.getElementById(containerId);
  if (!rows.length) {
    c.innerHTML = "<p style='padding:16px;color:var(--muted)'>No matches found. Try relaxing the thresholds.</p>";
    return;
  }
  const th = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;border-bottom:1px solid var(--border);text-align:left";
  const td = "padding:7px 10px;border-bottom:1px solid var(--border);font-size:12px;vertical-align:top";
  const sub = "color:var(--muted);font-size:11px";
  let h = `<table style="width:100%;border-collapse:collapse"><thead><tr>
    <th style="${th}">Trade</th><th style="${th}">${cfg.legA}</th><th style="${th}">${cfg.legB}</th>
    <th style="${th};text-align:right">Edge</th><th style="${th};text-align:right">Size</th>
  </tr></thead><tbody>`;

  rows.forEach((r, i) => {
    // "Buy Warrant / Sell Option" and "Long TW / Short US" both split on the
    // slash into a bought leg and a sold leg — colour them by side.
    const legs = String(r.trade || "").split("/").map(s => s.trim()).filter(Boolean);
    const tradeHtml = legs.map(p =>
      `<div style="color:${/^(Buy|Long)/i.test(p) ? MARK_LONG : MARK_SHORT};font-size:11px;font-weight:600">${p}</div>`
    ).join("");
    const dbg = r.executable === false
      ? `<div style="${sub};color:var(--call)">debug</div>` : "";

    const tCls = r.type === "Call" ? "call" : "put";
    const typeTag = `<span class="${tCls}" style="font-weight:600">${r.type || ""}</span>`;
    const legCell = (title, K, dte) =>
      `<div style="font-weight:600">${title}</div>
       <div style="${sub}">K${_n(K)} · ${dte == null ? "—" : dte + "d"}</div>`;

    // Colour each figure by its own sign. In TW/US mode price_diff is the parity
    // gap while price_diff_pct is the entry credit, so the two genuinely disagree
    // in sign and must not share a class.
    const pd = r.price_diff, pct = r.price_diff_pct;
    const pdCls = pd > 0 ? "put" : "call";
    const pctCls = pct > 0 ? "put" : "call";
    const credit = r.entry_credit != null
      ? `<div style="${sub}">${r.entry_credit >= 0 ? "+" : ""}${_n(r.entry_credit, 0)} NT$</div>` : "";

    // Size = the quantity that actually has to be executed, plus whether the
    // best price level holds enough resting size to fill it.
    let sizeMain, sizeSub, fillOk;
    if (mode === "twus") {
      sizeMain = `${_n(r.tw_contracts, 0)} 口`;
      sizeSub = `${_n(r.us_contracts, 0)} US`;
      fillOk = r.tw_fillable;
    } else {
      sizeMain = `${_n(r.board_lots, 3)} 張`;
      sizeSub = r.warrant_depth_lots != null ? `depth ${_n(r.warrant_depth_lots, 0)}` : "";
      fillOk = r.fillable;
    }
    const fillTag = fillOk == null ? ""
      : fillOk ? `<span class="put" style="font-weight:600">✓</span>`
               : `<span class="call" style="font-weight:600">✗</span>`;

    h += `<tr style="cursor:pointer" onclick="${cfg.open}(${arrName}[${i}])" title="Click for the full breakdown">
      <td style="${td}">${tradeHtml}${dbg}</td>
      <td style="${td}">${typeTag} ${legCell(r[cfg.aName] || r.warrant_code || "", r[cfg.aK], r[cfg.aD])}</td>
      <td style="${td}">${legCell(r[cfg.bName] || "", r[cfg.bK], r[cfg.bD])}</td>
      <td style="${td};text-align:right">
        <div class="${pdCls}" style="font-weight:700">${pd == null ? "—" : (pd > 0 ? "+" : "") + _n(pd, 4)}</div>
        <div class="${pctCls}" style="font-size:11px">${pct == null ? "" : (pct > 0 ? "+" : "") + _n(pct) + "%"}</div>
        ${credit}
      </td>
      <td style="${td};text-align:right">
        <div style="font-weight:600;${fillOk === false ? "color:var(--call)" : ""}">${sizeMain} ${fillTag}</div>
        <div style="${sub}">${sizeSub}</div>
      </td></tr>`;
  });
  c.innerHTML = h + "</tbody></table>";
}

function renderArbTable(rows) {
  renderCompactArbTable("arb-tableContainer", rows, "direct", "currentArbData");
}

function sortArbBy(key, asc) {
  if (!currentArbData.length) return;
  currentArbData.sort((a, b) => {
    const av = a[key] ?? (asc ? Infinity : -Infinity);
    const bv = b[key] ?? (asc ? Infinity : -Infinity);
    return asc ? av - bv : bv - av;
  });
  renderArbTable(currentArbData);
}

// ── Straddle Vol Arb ───────────────────────────────────────────────
let currentStraddleData = [];

function runStraddle() { fetchStraddleData(); }

function getStraddleFilters() {
  const sel = document.getElementById("straddleStockSelect");
  return {
    stock_codes: Array.from(sel.selectedOptions).map(o => o.value),
    max_strike_diff_pct: parseFloat(document.getElementById("straddleMaxStrikePct").value) || 10,
    max_dte_diff: parseInt(document.getElementById("straddleMaxDteDiff").value) || 30,
    min_iv_edge: parseFloat(document.getElementById("straddleMinIvEdge").value) || 0,
    min_volume: parseInt(document.getElementById("straddleMinVolume").value) || 0,
    loose: document.getElementById("straddleLoose").checked,
    short_warrants: document.getElementById("straddleShortWarrants").checked,
    require_dte_cover: document.getElementById("straddleDteCover").checked,
  };
}

async function fetchStraddleData() {
  const st = document.getElementById("straddle-status");
  st.textContent = "Fetching warrants and options…";
  document.getElementById("straddle-tableContainer").innerHTML = "";
  document.getElementById("straddleDownloadBtn").style.display = "none";
  let data;
  try {
    const res = await api("/straddle_arbitrage", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getStraddleFilters()),
    });
    data = await res.json();
  } catch (e) { st.textContent = "Error: " + e.message; return; }
  if (data.error) { st.textContent = "Error: " + data.error; return; }
  currentStraddleData = data.rows;
  const f = getStraddleFilters();
  const dbg = [];
  if (f.loose) dbg.push("loose prices");
  if (f.short_warrants) dbg.push("short warrants");
  if (!f.require_dte_cover) dbg.push("DTE cover off");
  const warn = dbg.length
    ? `<div style="margin-top:6px;padding:6px 10px;border-radius:4px;background:rgba(220,80,60,0.15);border:1px solid rgba(220,80,60,0.5);color:var(--text);font-size:12px"><b>DEBUG MODE — rows below are NOT executable</b> (${dbg.join(", ")}).</div>`
    : "";
  st.innerHTML = `${data.count} straddle pairs — <b>long</b> the cheap-vol package (buy) vs <b>short</b> the dear option package (write). Edge = short IV − long IV (vol pts).${asOfLabel(data)}` + warn;
  if (data.count) document.getElementById("straddleDownloadBtn").style.display = "inline-block";
  renderStraddleTable(currentStraddleData);
}

function _strLegCell(lg) {
  const tag = lg.source === "warrant" ? "W" : "O";
  const col = lg.type === "Call" ? "var(--put)" : "var(--call)";
  return `<span style="color:${col};font-weight:600">${tag}·${lg.type[0]}</span> K${lg.K} ${lg.dte}d <span style="color:var(--muted)">iv${(lg.iv*100).toFixed(1)}</span>`;
}

function renderStraddleTable(rows) {
  const c = document.getElementById("straddle-tableContainer");
  if (!rows.length) {
    c.innerHTML = "<p style='padding:16px;color:var(--muted)'>No straddle pairs. Relax Δ-strike / ΔDTE, or lower Min IV Edge — warrants trade rich, so a positive (executable) edge is uncommon.</p>";
    return;
  }
  const th = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;border-bottom:1px solid var(--border);text-align:left";
  const td = "padding:7px 10px;border-bottom:1px solid var(--border);font-size:12px";
  let h = `<table style="width:100%;border-collapse:collapse"><thead><tr>
    <th style="${th}">Stock</th><th style="${th}">Long pkg (buy)</th><th style="${th}">Short pkg (write · options)</th>
    <th style="${th};text-align:right">IV edge</th><th style="${th};text-align:right">Long/Short IV</th>
    <th style="${th};text-align:right">Net cash</th><th style="${th};text-align:right">1st exp</th></tr></thead><tbody>`;
  rows.forEach((r, i) => {
    const eCol = r.iv_edge_pts >= 0 ? "var(--put)" : "var(--call)";
    h += `<tr style="cursor:pointer" onclick="openStraddleModal(currentStraddleData[${i}])" title="Click for breakdown">
      <td style="${td}">${r.underlying_code}</td>
      <td style="${td}">${_strLegCell(r.long_call)} + ${_strLegCell(r.long_put)}</td>
      <td style="${td}">${_strLegCell(r.short_call)} + ${_strLegCell(r.short_put)}</td>
      <td style="${td};text-align:right;font-weight:700;color:${eCol}">${r.iv_edge_pts>=0?"+":""}${r.iv_edge_pts.toFixed(2)}</td>
      <td style="${td};text-align:right">${(r.long_iv*100).toFixed(1)} / ${(r.short_iv*100).toFixed(1)}</td>
      <td style="${td};text-align:right;color:${r.net_cash>=0?'var(--put)':'var(--call)'}">${r.net_cash>=0?"+":""}${Math.round(r.net_cash).toLocaleString()}</td>
      <td style="${td};text-align:right">${r.first_expiry}d</td></tr>`;
  });
  c.innerHTML = h + "</tbody></table>";
}

function sortStraddleBy(key, asc) {
  if (!currentStraddleData.length) return;
  currentStraddleData.sort((a, b) => { const av = a[key] ?? (asc ? Infinity : -Infinity), bv = b[key] ?? (asc ? Infinity : -Infinity); return asc ? av - bv : bv - av; });
  renderStraddleTable(currentStraddleData);
}

async function downloadStraddleCSV() {
  const res = await api("/straddle_arbitrage_csv", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(getStraddleFilters()) });
  const blob = await res.blob(); const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = "straddle_arbitrage.csv"; a.click();
}

function openStraddleInfo() {
  alert("Straddle Vol Arb\n\nA straddle package = one call + one put on the same underlying (strikes may differ within Δ-strike = a strangle).\n\nLONG the cheapest-implied-vol package (legs bought from a warrant OR an option) and SHORT the dearest package (OPTION legs only — Taiwan warrants can't be shorted). Edge = short IV − long IV, in vol points.\n\nCompared in IMPLIED VOL, not price, so the deterministic intrinsic (strike) and √T (expiry) terms are stripped out. Because strikes/expiries differ (loose match) each trade carries residual net delta + net vega + skew/term exposure — see the breakdown. The trade unwinds at the FIRST expiry. Every leg is then settled at INTRINSIC — ITM exercised, OTM worthless — even legs with time left, so the P&L curve contains no IV input at all and rests only on payoff structure plus entry prices.\n\nThat approximation cuts both ways: discarding a long leg's remaining time value is conservative, but assuming a short leg that outlives first expiry is closed at intrinsic ignores the time value you'd actually pay to buy it back. The curve is only a true floor when every short leg expires at first expiry.\n\nWarrants trade rich, so a positive (executable) edge is uncommon. v1 uses the raw IV spread for ranking; skew/term adjustment is a later refinement.");
}

function closeStraddleModal() {
  document.getElementById("straddleModal").style.display = "none";
  Plotly.purge("straddle-payoff-chart");
}

function openStraddleModal(r) {
  const S = r.underlying_price, CS = r.contract_size;
  const legs = [
    { ...r.long_call, dir: +1, side: "Long" },
    { ...r.long_put, dir: +1, side: "Long" },
    { ...r.short_call, dir: -1, side: "Short" },
    { ...r.short_put, dir: -1, side: "Short" },
  ];
  // Net residual greeks TODAY (each leg at its own dte), per package (2000 sh).
  let nd = 0, nv = 0;
  legs.forEach(l => {
    const tau = l.dte / 365, isPut = l.type === "Put";
    nd += l.dir * CS * bsDelta(S, l.K, tau, R_FREE, l.iv, isPut);
    nv += l.dir * CS * bsVega(S, l.K, tau, R_FREE, l.iv);
  });
  const nvPt = nv / 100;   // NT$ per 1 vol-point

  const th = "padding:6px 9px;background:var(--surface);color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase;text-align:left";
  const td = "padding:6px 9px;border-bottom:1px solid var(--border);font-size:12px";
  const tdr = td + ";text-align:right;font-variant-numeric:tabular-nums";
  let legRows = "";
  legs.forEach(l => {
    const sc = l.side === "Long" ? "var(--put)" : "var(--call)";
    const sz = l.source === "warrant" ? `${l.board_lots} 張` : `${l.board_lots} contract`;
    legRows += `<tr>
      <td style="${td};color:${sc};font-weight:600">${l.side} ${l.type}</td>
      <td style="${td}">${l.source === "warrant" ? "Warrant" : "Option"} <span style="color:var(--muted)">${l.id}</span></td>
      <td style="${tdr}">${l.K}</td><td style="${tdr}">${l.dte}d</td>
      <td style="${tdr}">${(l.iv*100).toFixed(1)}%</td>
      <td style="${tdr}">${l.px_per_share}</td><td style="${tdr}">${sz}</td></tr>`;
  });

  // "Stub" legs = still alive at first expiry but settled at intrinsic in the
  // P&L. The approximation is not symmetric: for a LONG stub you give up time
  // value you actually own (understates P&L, safe); for a SHORT stub you assume
  // you buy it back at intrinsic when you'd really pay intrinsic + time value
  // (overstates P&L, unsafe). Long stubs are the margin of safety, short stubs
  // are unmodelled cost — so name them.
  const longStubs = legs.filter(l => l.dir > 0 && l.dte > r.first_expiry);
  const shortStubs = legs.filter(l => l.dir < 0 && l.dte > r.first_expiry);
  const stubTxt = (arr) => arr.map(l => `${l.type} ${l.dte}d`).join(", ");
  const stubNote =
    (longStubs.length
      ? `<b>Conservative:</b> long ${stubTxt(longStubs)} still ${longStubs.map(l => l.dte - r.first_expiry).join("/")}d from expiry — remaining time value discarded, so realised P&L should beat this curve. `
      : "")
    + (shortStubs.length
      ? `<b style="color:var(--call)">Unmodelled cost:</b> short ${stubTxt(shortStubs)} outlives first expiry by ${shortStubs.map(l => l.dte - r.first_expiry).join("/")}d — closing it costs intrinsic <i>plus</i> time value, which this curve omits, so the line is <b>not</b> a floor.`
      : `All short legs die at first expiry, so nothing is bought back above intrinsic — this curve is a genuine worst case.`);

  const eCol = r.iv_edge_pts >= 0 ? "var(--put)" : "var(--call)";
  // Executability is a property of the LEGS and the pricing mode, never of the
  // edge sign. A short warrant leg can't be written at all, and loose prices
  // don't cross the spread — either makes the row debug-only.
  const nx = [];
  if (r.shorts_warrant) nx.push("shorts a warrant — only the issuer can write these");
  if (r.loose_prices) nx.push("loose prices — spread not crossed");
  const exec = nx.length
    ? `<span style="color:var(--call);font-weight:700">NON-EXECUTABLE (debug): ${nx.join("; ")}</span>`
    : r.iv_edge_pts > 0
      ? '<span style="color:var(--put)">EXECUTABLE (short legs are options)</span>'
      : '<span style="color:var(--call)">no positive edge — long-vol is richer than short-vol here</span>';
  document.getElementById("straddle-modal-body").innerHTML = `
    <h2 style="margin:0 0 4px;font-size:18px">${r.underlying_code} — Straddle Vol Arb <span style="color:var(--muted);font-size:13px">spot ${S}</span></h2>
    <p style="margin:0 0 16px;font-size:12px;color:var(--muted)">Long the cheap-vol package, short the dear option package. ${exec}.</p>
    <div style="display:flex;gap:22px;flex-wrap:wrap;margin-bottom:18px">
      <div><div style="font-size:10px;color:var(--muted);text-transform:uppercase">IV edge</div><div style="font-size:20px;font-weight:700;color:${eCol}">${r.iv_edge_pts>=0?"+":""}${r.iv_edge_pts.toFixed(2)} pts</div></div>
      <div><div style="font-size:10px;color:var(--muted);text-transform:uppercase">Long / Short IV</div><div style="font-size:20px;font-weight:700">${(r.long_iv*100).toFixed(1)} / ${(r.short_iv*100).toFixed(1)}%</div></div>
      <div><div style="font-size:10px;color:var(--muted);text-transform:uppercase">Net cash (per 2000 sh)</div><div style="font-size:20px;font-weight:700;color:${r.net_cash>=0?'var(--put)':'var(--call)'}">${r.net_cash>=0?"+":""}${Math.round(r.net_cash).toLocaleString()}</div></div>
      <div><div style="font-size:10px;color:var(--muted);text-transform:uppercase">First expiry</div><div style="font-size:20px;font-weight:700">${r.first_expiry}d</div></div>
    </div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:16px"><thead><tr>
      <th style="${th}">Leg</th><th style="${th}">Instrument</th><th style="${th};text-align:right">Strike</th><th style="${th};text-align:right">DTE</th>
      <th style="${th};text-align:right">IV</th><th style="${th};text-align:right">Px/sh</th><th style="${th};text-align:right">Size</th></tr></thead>
      <tbody>${legRows}</tbody></table>
    <div style="display:flex;gap:22px;flex-wrap:wrap;margin-bottom:14px">
      <div><div style="font-size:10px;color:var(--muted);text-transform:uppercase">Residual net Δ</div><div style="font-size:16px;font-weight:700;color:${Math.abs(nd)<50?'var(--muted)':(nd>0?'var(--put)':'var(--call)')}">${nd>=0?"+":""}${Math.round(nd).toLocaleString()} sh</div></div>
      <div><div style="font-size:10px;color:var(--muted);text-transform:uppercase">Net vega</div><div style="font-size:16px;font-weight:700">${nvPt>=0?"+":""}${Math.round(nvPt).toLocaleString()} / vol-pt</div></div>
    </div>
    <div id="straddle-payoff-chart" style="width:100%;height:280px"></div>
    <div style="background:var(--surface);border-radius:6px;padding:11px 14px;margin-top:12px;font-size:11px;color:var(--muted);line-height:1.6">
      P&L at the FIRST expiry (${r.first_expiry}d) across spot. <b>Every leg is settled at intrinsic</b> — ITM legs exercised, OTM legs worthless — including legs with time left. No Black-Scholes, no IV anywhere in this curve, so it can't be distorted by the unreliable vols solved off near-intrinsic or deep-OTM quotes. ${stubNote} Because strikes/expiries differ this is a strangle-vs-strangle carrying the <b>net Δ</b> above. Warrant legs can't be shorted, so only option legs are written unless the debug toggle is on. Quotes are delayed; run during the TW session (09:00–13:30) for live two-sided option prices.
    </div>`;

  // Payoff at first expiry — every leg settled at INTRINSIC, including the ones
  // with time left. Deliberately not marked at Black-Scholes: the solved IVs are
  // unreliable on near-intrinsic and deep-OTM quotes, and an arb should stand on
  // payoff structure plus entry price alone, with no vol input in the P&L.
  //
  // Range must span every strike AND spot. A spot±20% window hides the kinks
  // whenever the legs are far from the money (deep-ITM warrants sit hundreds of
  // points below spot), rendering the payoff as a featureless flat line and
  // hiding exactly where the risk turns.
  const strikes = legs.map(l => l.K);
  const anchors = [S, ...strikes];
  let lo = Math.min(...anchors), hi = Math.max(...anchors);
  const pad = Math.max((hi - lo) * 0.18, S * 0.06);
  lo = Math.max(0, lo - pad); hi = hi + pad;

  // Sample the strikes exactly so each kink vertex lands on a plotted point
  // instead of being rounded off by the grid.
  const N = 160;
  const xset = new Set();
  for (let i = 0; i <= N; i++) xset.add(lo + (hi - lo) * i / N);
  anchors.forEach(a => { if (a > lo && a < hi) xset.add(a); });
  const xs = [...xset].sort((a, b) => a - b);
  const payoff = ST => legs.reduce((acc, l) => {
    const val = l.type === "Put" ? Math.max(0, l.K - ST) : Math.max(0, ST - l.K);
    return acc + l.dir * CS * (val - l.px_per_share);
  }, 0);
  const ys = xs.map(x => Math.round(payoff(x)));
  // Mark every strike so the kinks are identifiable — green a leg you own,
  // red one you owe — via the shared payoff-chart helper.
  const _sMk = payoffStrikeMarks(
    legs.map(l => ({ K: l.K, label: `${l.side[0]}${l.type[0]}`, dir: l.dir })), S);
  document.getElementById("straddleModal").style.display = "block";
  Plotly.react("straddle-payoff-chart", [
    { x: [lo, hi], y: [0, 0], mode: "lines", line: { color: "rgba(255,255,255,0.15)", dash: "dash", width: 1 }, showlegend: false, hoverinfo: "skip" },
    { x: xs, y: ys, mode: "lines", line: { color: "#4f8ef7", width: 2 }, hovertemplate: "spot %{x:.0f}<br>P&L %{y:+,.0f} NT$<extra></extra>", showlegend: false },
    { x: [S], y: [Math.round(payoff(S))], mode: "markers", marker: { color: "#fff", size: 7 }, hovertemplate: "spot now %{x:.0f}<br>P&L %{y:+,.0f} NT$<extra></extra>", showlegend: false },
  ], {
    shapes: _sMk.shapes, annotations: _sMk.annotations,
    paper_bgcolor: "#13161f", plot_bgcolor: "#0d0f16",
    font: { color: "#8b90a0", family: "Inter,system-ui,sans-serif", size: 11 },
    xaxis: { title: "underlying at first expiry", gridcolor: "#252836", zerolinecolor: "#252836" },
    yaxis: { title: "P&L (NT$)", gridcolor: "#252836", zerolinecolor: "#252836", tickformat: "+," },
    margin: { l: 60, r: 12, t: 10, b: 38 },
  }, { responsive: true });
}

// ── Direct Match Modal ─────────────────────────────────────────────

function openDirectModal(row) {
  const isPcp = "executable" in row;   // PCP rows carry the executable flag
  // Re-map Direct Match row fields to the shape openArbModal expects
  const mapped = {
    warrant_code:     row.warrant_code,
    warrant_name:     row.warrant_name,
    warrant_type:     row.type,
    opt_type:         isPcp ? row.opt_type : row.type,  // PCP pairs opposite type
    option_contract:  row.option_contract,
    underlying_price: row.underlying_price,
    warrant_dte:      row.warrant_dte,
    opt_dte:          row.opt_dte,
    dte_diff:         row.dte_diff,
    warrant_strike:   row.warrant_strike,
    opt_strike:       row.opt_strike,
    strike_diff_pct:  row.strike_diff_pct,
    warrants_needed:  row.warrants_needed,
    opt_contract_size:row.opt_contract_size,
    warrant_depth_lots: row.warrant_depth_lots,
    fillable:         row.fillable,
    warrant_ask:      row.warrant_ask,
    warrant_bid:      row.warrant_bid,
    opt_bid:          row.opt_bid,
    opt_ask:          row.opt_ask,
    warrant_per_share:row.warrant_per_share,
    opt_per_share:    row.opt_per_share,
    // Use price_diff as the arb signal (synthetic − warrant per share)
    pcp_diff:         row.price_diff,
    pcp_diff_pct:     row.price_diff_pct,
    // PCP: real synthetic + bond PV from the backend. Same-type: synthetic
    // = the option price itself (no conversion), no bond leg.
    synthetic_price:  isPcp ? row.synthetic_price : row.opt_per_share,
    bond_pv:          isPcp ? row.bond_pv : null,
    executable:       isPcp ? row.executable : true,
    warrant_iv:       row.warrant_iv,
    opt_iv:           row.opt_iv,
  };
  openArbModal(mapped, isPcp ? "pcp" : "direct");
}

// ── PCP Trade Modal ─────────────────────────────────────────────────

let _pcpRow = null;

let _contractSize = 2000;

let _pcpChartMode = "exact";   // "exact" (fractional 張) | "whole" (executable board lots)
// Entry snapshot for the Portfolio "Enter Trade" button.

let _pcpLegs = null, _pcpNetCf = 0, _pcpMode = null, _pcpEntry = null;

function openDirectInfo() {
  document.getElementById("directInfoModal").style.display = "block";
}

function openArbModal(row, mode) {
  _pcpRow = row;
  _pcpMode = mode;
  _pcpEntry = null;   // set by loadAdrScenario for us/twus; stays null for direct
  const isCall = row.warrant_type === "Call";
  const S0 = row.underlying_price;
  const Ko = row.opt_strike;
  const Kw = row.warrant_strike;
  const maxDte = row.opt_dte;
  const bondPv = Ko * Math.exp(-R_FREE * maxDte / 365);
  const n = row.warrants_needed;
  // Contract size = how many underlying shares one option contract controls
  const contractSize = row.opt_contract_size ?? 2000;
  _contractSize = contractSize;

  const isPcpMode = mode === "pcp" || mode === "uspcp";
  const modeLabel = mode === "twus" ? "TW Opt / US Opt" : mode === "us" ? "US Option Match"
    : mode === "direct" ? "Direct Match" : mode === "uspcp" ? "US PCP (cross-market)" : "PCP Arb";
  document.getElementById("pcp-modal-title").textContent =
    `${row.warrant_name} (${row.warrant_type}) ↔ ${row.option_contract} (${row.opt_type})  —  ${modeLabel}`;
  const pcpNonExec = isPcpMode && row.executable === false;
  // American early-assignment risk (uspcp only): the executable trade SHORTS
  // the US option (opposite type to the warrant). US ADR options are
  // American, so a short leg deep ITM can be assigned early — collapsing the
  // hedge before the long warrant expires. Measure the short leg's moneyness
  // off the ADR spot. (Non-executable rows short the warrant, not the option.)
  let usPcpAssign = "";
  if (mode === "uspcp" && row.executable !== false) {
    const adr = row.adr_underlying ?? S0;
    const shortIsPut = row.opt_type === "Put";
    const itmFrac = shortIsPut ? (Ko - adr) / Ko : (adr - Ko) / Ko;  // >0 = ITM
    if (itmFrac > 0.0) {
      usPcpAssign = `<br><span class="call" style="font-weight:700">⚠ Short US ${row.opt_type.toLowerCase()} is ITM by ${(itmFrac*100).toFixed(1)}% (ADR ${adr.toFixed(2)} vs strike ${Ko.toFixed(2)}) — American, real early-assignment risk; dividends / high USD rates raise it.</span>`;
    } else if (itmFrac > -0.03) {
      usPcpAssign = `<br><span style="font-weight:600;color:#fbbf24">Short US ${row.opt_type.toLowerCase()} is near-the-money (${(itmFrac*100).toFixed(1)}%) — American; watch for early assignment if it moves ITM.</span>`;
    }
  }
  document.getElementById("pcp-legs-caption").innerHTML =
    (mode === "twus"
      ? `Trade Legs (matched ${contractSize.toLocaleString()} TW shares = ${row.tw_contracts} TW contract × 2,000 = ${row.us_contracts} US contracts × 500)`
      : `Trade Legs (per 1 options contract = ${contractSize.toLocaleString()} ${(mode === "us" || mode === "uspcp") ? "TW shares" : "shares"})`)
    + (pcpNonExec ? ` <span class="call" style="font-weight:700">— NON-EXECUTABLE (warrants can't be shorted; debug only)</span>` : "")
    + (isPcpMode ? `<br><span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted)">European PCP, dividends ignored — a pair whose expiry crosses an ex-dividend date is mispriced (synthetic call biased high / put low).${mode === "uspcp" ? ` The short US leg is <b>American</b>.` : ""}</span>` : "")
    + usPcpAssign;

  // ── Trade legs ──────────────────────────────────────────────────
  let legs;
  if (mode === "twus") {
    // Two option legs, same matched share count. warrant_* = TW leg,
    // opt_* = US leg; warrant_per_share / opt_per_share already hold the
    // executable price for the trade's direction (sell rich / buy cheap).
    const twShares = row.tw_contracts > 0 ? contractSize / row.tw_contracts : 2000;
    const usShares = row.us_contracts > 0 ? contractSize / row.us_contracts : 500;
    const twLabel = `${row.tw_contracts} TW contract${row.tw_contracts>1?"s":""} (×${twShares.toLocaleString()} sh) — ${row.warrant_code} ${row.warrant_type.toLowerCase()}`;
    const usLabel = `${row.us_contracts} US contract${row.us_contracts>1?"s":""} (×${usShares.toLocaleString()} TW sh) — ${row.option_contract}`;
    const longTw = row.pcp_diff >= 0;   // Long TW / Short US
    legs = [
      { action: longTw ? "BUY" : "SELL", instrument: twLabel, qty: n,
        price: row.warrant_per_share, cf: (longTw ? -1 : 1) * n * row.warrant_per_share },
      { action: longTw ? "SELL" : "BUY", instrument: usLabel, qty: contractSize,
        price: row.opt_per_share, cf: (longTw ? 1 : -1) * contractSize * row.opt_per_share },
    ];
  } else if (mode === "direct" || mode === "us") {
    // Simple 2-leg trade: buy cheaper warrant, sell pricier same-type option
    const wLabel = `${(n/1000).toLocaleString(undefined,{maximumFractionDigits:3})} 張 (${n.toLocaleString()} units) × ${row.warrant_code} (${row.warrant_type.toLowerCase()} warrant)`;
    const oLabel = `${row.option_contract} ${row.opt_type.toLowerCase()} option (×${contractSize.toLocaleString()} shares)`;
    // Loose (positive) prices the legs the way the backend price_diff did:
    // buy the warrant at its BID, sell the option at its ASK. Tight uses
    // the executable side: buy warrant at ASK, sell option at BID.
    const looseEl = mode === "us"
      ? document.getElementById("usPositiveLoose")
      : document.getElementById("arbPositiveLoose");
    const loose = !!(looseEl && looseEl.checked);
    if (row.pcp_diff >= 0) {
      // Warrant cheaper → buy warrant, sell option
      const buyPrice  = loose ? (row.warrant_bid ?? row.warrant_ask) : row.warrant_ask;
      const optSellPrice = loose ? row.opt_ask : (row.opt_bid ?? row.opt_per_share);
      legs = [
        { action:"BUY",  instrument:wLabel,  qty:n,    price:buyPrice,  cf:-(n*buyPrice) },
        { action:"SELL", instrument:oLabel,  qty:contractSize, price:optSellPrice, cf: contractSize*optSellPrice },
      ];
    } else {
      // Option cheaper → buy option at ask, sell warrant at bid
      const wBid = row.warrant_bid ?? row.warrant_ask;
      legs = [
        { action:"SELL", instrument:wLabel,  qty:n,    price:wBid,         cf: n*wBid },
        { action:"BUY",  instrument:oLabel,  qty:contractSize, price:row.opt_ask, cf:-contractSize*row.opt_ask },
      ];
    }
  } else {
    // PCP: 4-leg synthetic. Executable = long warrant / short synthetic;
    // non-executable = short warrant / long synthetic (every leg flipped).
    // Warrant priced per unit (ask when buying, bid when shorting); option
    // uses opt_per_share (bid for exec, ask for non-exec, set by backend).
    const executable = row.executable !== false;
    // Loose applies only to the executable (tradable) side. Loose buys the
    // warrant at its BID (favorable); tight buys at ASK. opt_per_share
    // already carries the loose/tight option side from the backend.
    const pcpLoose = executable && !!document.getElementById("arbPositiveLoose").checked;
    const pcpBondPv = row.bond_pv ?? bondPv;
    const wPrice = executable
      ? (pcpLoose ? (row.warrant_bid ?? row.warrant_ask) : row.warrant_ask)
      : (row.warrant_bid ?? row.warrant_ask);
    const optPrice = row.opt_per_share;
    const cs = contractSize;
    // Cross-market (uspcp): the stock/synthetic leg is the US ADR (converted
    // to TWD/TW-share), which differs from the warrant's local spot by the
    // ADR premium. Single-market pcp: both are the same underlying.
    const stockSpot = row.adr_underlying ?? S0;
    const isUsPcp = mode === "uspcp";
    const wLabel = `${(n/1000).toLocaleString(undefined,{maximumFractionDigits:3})} 張 (${n.toLocaleString()} units) × ${row.warrant_code} (${row.warrant_type.toLowerCase()} warrant)`;
    const stockLabel = `${cs.toLocaleString()} ${isUsPcp ? "TW-equiv shares via US ADR" : "shares"} ${row.warrant_name || row.warrant_code}`;
    const optLabel = `${row.option_contract} ${row.opt_type.toLowerCase()} option (×${cs.toLocaleString()} shares)`;
    const bondCcy = isUsPcp ? "USD" : "";
    const lendLabel = `Bond${bondCcy?" "+bondCcy:""} (lend ${pcpBondPv.toFixed(2)} → receive ${Ko.toFixed(0)} × ${cs.toLocaleString()} at expiry)`;
    const borrowLabel = `Bond${bondCcy?" "+bondCcy:""} (borrow ${pcpBondPv.toFixed(2)} → repay ${Ko.toFixed(0)} × ${cs.toLocaleString()} at expiry)`;
    if (executable) {
      legs = isCall ? [
        { action:"BUY",   instrument:wLabel,     qty:n,   price:wPrice,     cf:-(n*wPrice) },
        { action:"SHORT", instrument:stockLabel, qty:-cs, price:stockSpot,  cf: cs*stockSpot },
        { action:"LEND",  instrument:lendLabel,  qty:cs,  price:pcpBondPv,  cf:-(cs*pcpBondPv) },
        { action:"SELL",  instrument:optLabel,   qty:cs,  price:optPrice,   cf: cs*optPrice },
      ] : [
        { action:"BUY",   instrument:wLabel,      qty:n,  price:wPrice,     cf:-(n*wPrice) },
        { action:"BUY",   instrument:stockLabel,  qty:cs, price:stockSpot,  cf:-cs*stockSpot },
        { action:"BORROW",instrument:borrowLabel, qty:cs, price:pcpBondPv,  cf: cs*pcpBondPv },
        { action:"SELL",  instrument:optLabel,    qty:cs, price:optPrice,   cf: cs*optPrice },
      ];
    } else {
      legs = isCall ? [
        { action:"SHORT", instrument:wLabel,      qty:-n, price:wPrice,     cf: n*wPrice },
        { action:"BUY",   instrument:stockLabel,  qty:cs, price:stockSpot,  cf:-cs*stockSpot },
        { action:"BORROW",instrument:borrowLabel, qty:cs, price:pcpBondPv,  cf: cs*pcpBondPv },
        { action:"BUY",   instrument:optLabel,    qty:cs, price:optPrice,   cf:-cs*optPrice },
      ] : [
        { action:"SHORT", instrument:wLabel,     qty:-n,  price:wPrice,     cf: n*wPrice },
        { action:"SHORT", instrument:stockLabel, qty:-cs, price:stockSpot,  cf: cs*stockSpot },
        { action:"LEND",  instrument:lendLabel,  qty:cs,  price:pcpBondPv,  cf:-(cs*pcpBondPv) },
        { action:"BUY",   instrument:optLabel,   qty:cs,  price:optPrice,   cf:-cs*optPrice },
      ];
    }
  }
  const netCf = legs.reduce((s,l)=>s+l.cf, 0);
  _pcpLegs = legs; _pcpNetCf = netCf;   // snapshot for Enter Trade

  const actionColor = a => (a==="BUY"||a==="LEND"||a==="BORROW") ? "var(--put)" : "var(--call)";
  let tbody = "";
  legs.forEach(l=>{
    tbody+=`<tr>
      <td style="padding:6px 10px;border-bottom:1px solid var(--border);color:${actionColor(l.action)};font-weight:600">${l.action}</td>
      <td style="padding:6px 10px;border-bottom:1px solid var(--border);color:var(--text)">${l.instrument}</td>
      <td style="padding:6px 10px;border-bottom:1px solid var(--border);color:var(--text);text-align:right">${Number(l.qty).toLocaleString()}</td>
      <td style="padding:6px 10px;border-bottom:1px solid var(--border);color:var(--text);text-align:right">${Number(l.price).toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td style="padding:6px 10px;border-bottom:1px solid var(--border);color:${l.cf>=0?"var(--put)":"var(--call)"};text-align:right;font-variant-numeric:tabular-nums">${l.cf>=0?"+":""}${Math.round(l.cf).toLocaleString()}</td>
    </tr>`;
  });
  document.getElementById("pcp-legs-body").innerHTML = tbody;
  document.getElementById("pcp-legs-foot").innerHTML = `
    <tr>
      <td colspan="4" style="padding:8px 10px;font-weight:600;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px">Net premium received</td>
      <td style="padding:8px 10px;font-weight:700;font-size:14px;color:${netCf>=0?"var(--put)":"var(--call)"};text-align:right;font-variant-numeric:tabular-nums">${netCf>=0?"+":""}${Math.round(netCf).toLocaleString()} NT$</td>
    </tr>`;

  // ── Orderbook depth (warrant leg) ───────────────────────────────
  // Only Direct Match + PCP carry warrant depth (best-level resting size
  // from CMoney). Show whether the needed board lots fill at the quote.
  const depthPanel = document.getElementById("pcp-depth-panel");
  // Direct/US same-type + PCP/US-PCP all trade a warrant leg (CMoney depth);
  // twus is option-vs-option and is handled by its own two-leg panel below.
  const warrantModeDepth = ["direct", "pcp", "us", "uspcp"].includes(mode);
  if (row.warrant_depth_lots != null && warrantModeDepth) {
    const depth = row.warrant_depth_lots;                 // 張 resting at best level
    const lotsNeeded = row.warrants_needed / 1000;        // 張 needed (fractional)
    // Executable PCP + cheap-warrant same-type both BUY the warrant (ask side);
    // the warrant-rich same-type direction SELLS it (bid side).
    const buyWarrant = (mode === "pcp" || mode === "uspcp") ? (row.executable !== false) : (row.pcp_diff >= 0);
    const side = buyWarrant ? "best ask (resting sells)" : "best bid (resting buys)";
    const ok = !!row.fillable;
    const fillPct = depth > 0 ? Math.min(100, depth / lotsNeeded * 100) : 0;
    depthPanel.style.display = "block";
    depthPanel.style.background = ok ? "rgba(34,197,94,.08)" : "rgba(239,68,68,.08)";
    depthPanel.style.border = `1px solid ${ok ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)"}`;
    depthPanel.innerHTML =
      `<div style="font-weight:600;text-transform:uppercase;letter-spacing:.3px;font-size:11px;color:var(--muted);margin-bottom:4px">Orderbook depth — warrant leg</div>`
      + `<b style="color:${ok ? "var(--put)" : "var(--call)"}">${ok ? "✓ Fillable at quote" : "✗ NOT fillable at quote"}</b> · `
      + `<b>${depth.toLocaleString()} 張</b> resting at ${side}, need <b>${lotsNeeded.toLocaleString(undefined,{maximumFractionDigits:3})} 張</b>`
      + (ok ? "" : ` — only ${fillPct.toFixed(0)}% covers at the quote; rest must walk the book (worse price, edge shrinks) or split the order over time.`)
      + `<div style="color:var(--muted);font-size:11px;margin-top:4px">Best-level size only (五檔 level 1); size can vanish before you fill — a gate, not a guarantee.</div>`;
  } else if (mode === "twus") {
    // Two option legs, no warrant. TW leg has real best-level size (口) from
    // TAIFEX MIS; US leg (yfinance) has no size — volume + OI stand in as a
    // liquidity proxy only.
    const longTw = row.pcp_diff >= 0;                 // Long TW / Short US
    const twSide = longTw ? "buy @ ask" : "sell @ bid";
    const twDepth = row.tw_depth_contracts;           // 口 resting, TW side
    const twNeed  = row.tw_contracts;
    const usNeed  = row.us_contracts;
    const twOk = !!row.tw_fillable;
    const twKnown = twDepth != null;
    const usVol = row.us_volume ?? 0, usOi = row.us_oi ?? 0;
    // US proxy verdict: OI/volume comfortably above contracts needed = likely ok.
    const usThin = Math.max(usVol, usOi) < usNeed;
    const overallOk = twKnown && twOk;                // only the TW leg is a hard gate
    depthPanel.style.display = "block";
    depthPanel.style.background = overallOk ? "rgba(34,197,94,.08)" : "rgba(239,68,68,.08)";
    depthPanel.style.border = `1px solid ${overallOk ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)"}`;
    const twVerdict = !twKnown
      ? `<span style="color:var(--muted)">size —  (off-hours / no MIS quote)</span>`
      : `<b style="color:${twOk?"var(--put)":"var(--call)"}">${twOk?"✓":"✗"} ${twDepth.toLocaleString()} 口</b> resting (${twSide}), need <b>${twNeed} 口</b>`;
    depthPanel.innerHTML =
      `<div style="font-weight:600;text-transform:uppercase;letter-spacing:.3px;font-size:11px;color:var(--muted);margin-bottom:4px">Orderbook depth — both option legs</div>`
      + `<div><b>TW leg:</b> ${twVerdict}</div>`
      + `<div style="margin-top:2px"><b>US leg:</b> need <b>${usNeed} contract${usNeed>1?"s":""}</b> · `
      + `<span style="color:${usThin?"var(--call)":"var(--muted)"}">vol ${usVol.toLocaleString()}, OI ${usOi.toLocaleString()}</span> `
      + `<span style="color:var(--muted)">(proxy — yfinance gives no bid/ask size)</span></div>`
      + `<div style="color:var(--muted);font-size:11px;margin-top:4px">TW size = TAIFEX MIS best level (口, ~20 min delayed). US leg has no true depth; treat vol/OI as a rough liquidity read, not resting size.</div>`;
  } else {
    depthPanel.style.display = "none";
  }

  // ── Net residual greeks (Δ + Vega) ──────────────────────────────
  renderGreeks(row);

  // ── Comparison table ────────────────────────────────────────────
  const fmt = (v, dec=2) => v == null ? "—" : Number(v).toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec});
  const fmtPct = (v, dec=2) => v == null ? "—" : (v>=0?"+":"")+fmt(v,dec)+"%";
  const fmtNt = (v) => v == null ? "—" : (v>=0?"+":"")+Math.round(v).toLocaleString()+" NT$";

  // Row 1: Option (raw market price of the option leg)
  // Row 2: Warrant (raw, normalised per share)
  // Row 3: Difference after PCP — option becomes synthetic same-type as warrant
  const optPrice  = row.opt_per_share ?? row.opt_ask;   // option leg price actually traded (bid/ask per direction)
  const wPrice    = row.warrant_per_share; // warrant price per share
  const synthPrice= row.synthetic_price;   // option converted to same type as warrant via PCP
  const pcpDiff   = row.pcp_diff;          // synthPrice − wPrice
  const strikeDiff= row.opt_strike - row.warrant_strike;
  const dteDiffV  = row.opt_dte - row.warrant_dte;

  const thS = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border)";
  const tdL = "padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text)";
  const tdR = tdL+";text-align:right;font-variant-numeric:tabular-nums";
  const tdRm = tdL+";text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)";

  const diffColor = v => v == null ? "var(--text)" : v > 0 ? "var(--put)" : v < 0 ? "var(--call)" : "var(--muted)";

  const wRatio = row.warrants_needed > 0 ? (contractSize / row.warrants_needed) : null;

  document.getElementById("pcp-compare-table").innerHTML = `
    <thead><tr>
      <th style="${thS};text-align:left"></th>
      <th style="${thS};text-align:left">Type</th>
      <th style="${thS};text-align:right">Strike</th>
      <th style="${thS};text-align:right">DTE</th>
      <th style="${thS};text-align:right">Exercise Ratio</th>
      <th style="${thS};text-align:right">Price / share</th>
      <th style="${thS};text-align:right">Contract cost (×${contractSize.toLocaleString()})</th>
    </tr></thead>
    <tbody>
      <tr>
        <td style="${tdL};color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.3px">Option</td>
        <td style="${tdL}"><span class="${row.opt_type==='Call'?'call':'put'}">${row.opt_type}</span></td>
        <td style="${tdR}">${Number(row.opt_strike).toLocaleString()}</td>
        <td style="${tdR}">${row.opt_dte}d</td>
        <td style="${tdR}">${contractSize.toLocaleString()} shares/contract</td>
        <td style="${tdR}">${fmt(optPrice,4)}</td>
        <td style="${tdR}">${Math.round(optPrice*contractSize).toLocaleString()}</td>
      </tr>
      <tr>
        <td style="${tdL};color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.3px">Warrant</td>
        <td style="${tdL}"><span class="${row.warrant_type==='Call'?'call':'put'}">${row.warrant_type}</span></td>
        <td style="${tdR}">${Number(row.warrant_strike).toLocaleString()}</td>
        <td style="${tdR}">${row.warrant_dte}d</td>
        <td style="${tdR}">${wRatio!=null?wRatio.toFixed(4)+" shares/warrant":"—"}</td>
        <td style="${tdR}">${fmt(wPrice,4)}</td>
        <td style="${tdR}">${Math.round(wPrice*contractSize).toLocaleString()}</td>
      </tr>
      <tr style="background:var(--surface)">
        <td style="${tdL};color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.3px">PCP diff</td>
        <td style="${tdRm}">—</td>
        <td style="${tdR};color:${diffColor(strikeDiff)}">${strikeDiff>=0?"+":""}${Number(strikeDiff).toLocaleString()}</td>
        <td style="${tdR};color:${diffColor(dteDiffV)}">${dteDiffV>=0?"+":""}${dteDiffV}d</td>
        <td style="${tdRm}">—</td>
        <td style="${tdR};color:${diffColor(pcpDiff)};font-weight:600">${fmt(pcpDiff,4)} <span style="font-size:10px;opacity:.7">(${fmtPct(row.pcp_diff_pct)})</span></td>
        <td style="${tdR};color:${diffColor(pcpDiff)};font-weight:600">${Math.round(pcpDiff*contractSize)>=0?"+":""}${Math.round(pcpDiff*contractSize).toLocaleString()}</td>
      </tr>
    </tbody>`;

  // ── Slider ──────────────────────────────────────────────────────
  const slider = document.getElementById("pcp-dte-slider");
  slider.max = maxDte; slider.value = maxDte;
  document.getElementById("pcp-dte-label").textContent = `${maxDte} days`;

  // ── Chart ───────────────────────────────────────────────────────
  setPcpChartMode(_pcpChartMode);   // syncs button highlight + renders at current slider value

  // US + TW/US modes: show ADR-premium risk chart + Expected-Value model.
  // TW/US mode also shows the conditional "enter now" scenario EDA.
  const premSection = document.getElementById("us-premium-section");
  const scenSection = document.getElementById("us-scenario-section");
  if ((mode === "us" || mode === "twus" || mode === "uspcp") && row.us_stock_code) {
    premSection.style.display = "block";
    scenSection.style.display = "block";
    loadAdrPremium(row);
    loadAdrScenario(row);   // conditional EV — now the single EV for both tabs
  } else {
    premSection.style.display = "none";
    scenSection.style.display = "none";
  }

  // Non-executable PCP debug rows can't be traded — disable Enter Trade.
  const enterBtn = document.getElementById("pcp-enter-trade");
  if (pcpNonExec) {
    enterBtn.disabled = true;
    enterBtn.style.opacity = "0.4";
    enterBtn.style.cursor = "not-allowed";
    enterBtn.title = "Non-executable (short warrant) — debug view only";
  } else {
    enterBtn.disabled = false;
    enterBtn.style.opacity = "";
    enterBtn.style.cursor = "pointer";
    enterBtn.title = "";
  }

  document.getElementById("pcpModal").style.display = "block";
}

function closePcpModal() {
  document.getElementById("pcpModal").style.display = "none";
  _pcpRow = null;
}

document.getElementById("pcpModal").addEventListener("click", e => {
  if (e.target === document.getElementById("pcpModal")) closePcpModal();
});

function onPcpSlider(val) {
  document.getElementById("pcp-dte-label").textContent = `${val} days`;
  renderPcpChart(parseInt(val));
}

function setPcpChartMode(mode) {
  _pcpChartMode = mode;
  // Highlight the active button
  const ex = document.getElementById("pcp-mode-exact");
  const wh = document.getElementById("pcp-mode-whole");
  ex.style.fontWeight = mode === "exact" ? "600" : "400";
  wh.style.fontWeight = mode === "whole" ? "600" : "400";
  ex.style.borderColor = mode === "exact" ? "var(--accent)" : "";
  wh.style.borderColor = mode === "whole" ? "var(--accent)" : "";
  renderPcpChart(parseInt(document.getElementById("pcp-dte-slider").value));
}

// Net residual greeks of the paired position, at exact and whole-張 sizing.
// Everything is in underlying-share space (matches the chart), so per-share
// BS greeks × share counts net directly; the warrant's exercise ratio is
// baked into warrantShares (lots*1000*ratio).
//
// The option/synthetic leg greeks are evaluated with the WARRANT's put/call
// type at the option strike Ko. For same-type modes (direct/us/twus) that's
// just the option itself. For PCP the executable short leg is a synthetic
// (option + stock ± bond) that replicates a warrant-type option at Ko, and
// its delta = opposite-option delta ± 1 (put-call parity) — i.e. the stock
// leg is folded in automatically, so no separate stock term is needed.
// Vega is type-independent (put-call vega parity), so it needs no such care.

function renderGreeks(row) {
  const panel = document.getElementById("pcp-greeks-panel");
  const isPut = row.warrant_type === "Put";
  const ivW = row.warrant_iv, ivO = row.opt_iv;
  if (ivW == null || ivO == null) {
    panel.style.display = "block";
    panel.innerHTML =
      `<div style="font-weight:600;text-transform:uppercase;letter-spacing:.3px;font-size:11px;color:var(--muted);margin-bottom:4px">Net residual greeks</div>`
      + `<span style="color:var(--muted)">IV unavailable on a leg — can't mark greeks.</span>`;
    return;
  }
  const S0 = row.underlying_price;
  const optSpot = row.adr_underlying ?? S0;
  const Kw = row.warrant_strike, Ko = row.opt_strike;
  const tauW = (row.warrant_dte || row.opt_dte) / 365, tauO = row.opt_dte / 365;
  const dir = row.pcp_diff >= 0 ? 1 : -1;
  const contractSize = row.opt_contract_size ?? 2000;
  const optionShares = contractSize;
  const exactUnits = row.warrants_needed;
  const ratio = exactUnits > 0 ? contractSize / exactUnits : 1;
  const wholeLots = Math.max(1, Math.round(exactUnits / 1000));
  const wsExact = contractSize;
  const wsWhole = wholeLots * 1000 * ratio;

  // Per-share greeks per leg (option leg uses the warrant's type at Ko).
  const dW = bsDelta(S0, Kw, tauW, R_FREE, ivW, isPut);
  const vW = bsVega(S0, Kw, tauW, R_FREE, ivW);
  const dO = bsDelta(optSpot, Ko, tauO, R_FREE, ivO, isPut);
  const vO = bsVega(optSpot, Ko, tauO, R_FREE, ivO);
  // Long warrant / short option (dir flips for the reversed trade), mirroring
  // the chart's pnlAt sign structure.
  const netDelta = ws => dir * (ws * dW - optionShares * dO);
  const netVega  = ws => dir * (ws * vW - optionShares * vO);

  const fmtSigned = (v, dec=0) => (v >= 0 ? "+" : "") + Number(v).toLocaleString(undefined,{maximumFractionDigits:dec, minimumFractionDigits:dec});
  const row1 = (label, ws, note) => {
    const nd = netDelta(ws), nv = netVega(ws) / 100;   // vega per vol-point (NT$)
    const ndPct = optionShares ? nd / optionShares * 100 : 0;
    const dColor = Math.abs(ndPct) >= 2 ? "var(--call)" : "var(--text)";
    const vColor = Math.abs(nv) < 1 ? "var(--muted)" : (nv < 0 ? "var(--call)" : "var(--put)");
    const vTag = Math.abs(nv) < 1 ? "flat" : (nv < 0 ? "net short vol" : "net long vol");
    return `<tr>
      <td style="padding:4px 10px 4px 0;color:var(--muted)">${label}</td>
      <td style="padding:4px 14px;text-align:right;font-variant-numeric:tabular-nums;color:${dColor};font-weight:600">${fmtSigned(nd)} sh</td>
      <td style="padding:4px 14px;text-align:right;color:var(--muted);font-size:11px">≈${fmtSigned(ndPct,1)}% of hedge</td>
      <td style="padding:4px 14px;text-align:right;font-variant-numeric:tabular-nums;color:${vColor};font-weight:600">${fmtSigned(nv)} </td>
      <td style="padding:4px 0;color:${vColor};font-size:11px">${vTag}</td>
    </tr>`;
  };
  // twus has no 張 rounding (both legs contract-matched), so whole == exact.
  const sameWholeExact = Math.abs(wsWhole - wsExact) < 1e-9;
  panel.style.display = "block";
  panel.innerHTML =
    `<div style="font-weight:600;text-transform:uppercase;letter-spacing:.3px;font-size:11px;color:var(--muted);margin-bottom:6px">Net residual greeks — position (Δ shares, Vega NT$/vol-pt)</div>`
    + `<table style="border-collapse:collapse;font-size:12.5px"><tbody>`
    + row1(sameWholeExact ? "Matched" : "Whole 張 (executable)", wsWhole)
    + (sameWholeExact ? "" : row1("Exact hedge (fractional 張)", wsExact))
    + `</tbody></table>`
    + `<div style="color:var(--muted);font-size:11px;margin-top:6px">Δ = leftover directional exposure from strike/DTE mismatch${sameWholeExact ? "" : " + board-lot rounding"}; Vega = vol exposure the hedge doesn't cover (legs carry different IV). Marked at each leg's current spot with the solved per-leg IV.</div>`;
}

function renderPcpChart(dte) {
  const row = _pcpRow;
  const contractSize = _contractSize;   // option leg controls exactly this many shares
  const S0 = row.underlying_price;
  const tau = dte / 365;
  // dir flips the sign so the chart always shows P&L from the correct
  // trade direction: buy cheap / sell expensive.
  // pnlPerShare assumes long warrant + short option; negate when reversed.
  const dir = row.pcp_diff >= 0 ? 1 : -1;

  // Share counts per leg. Exact = matched 2000-share hedge (fractional 張).
  // Whole = round the warrant to executable board lots (張), leaving residual delta.
  const exactUnits = row.warrants_needed;          // 2000/ratio units (fractional 張)
  const ratio = exactUnits > 0 ? contractSize / exactUnits : 1;
  const optionShares = contractSize;
  let warrantShares, hedgeNote;
  if (_pcpChartMode === "whole") {
    const wholeLots = Math.max(1, Math.round(exactUnits / 1000));
    warrantShares = wholeLots * 1000 * ratio;
    const exactLots = exactUnits / 1000;
    const notionGap = Math.round(warrantShares - optionShares);
    hedgeNote = `Whole 張: ${wholeLots} 張 vs ${exactLots.toLocaleString(undefined,{maximumFractionDigits:3})} exact `
      + `— ${notionGap >= 0 ? "+" : ""}${notionGap.toLocaleString()} shares notional gap (see Net residual greeks above for the true Δ)`;
  } else {
    warrantShares = contractSize;
    hedgeNote = `Exact hedge: ${(exactUnits/1000).toLocaleString(undefined,{maximumFractionDigits:3})} 張 `
      + `(${exactUnits.toLocaleString()} units) — matched, no residual delta`;
  }
  document.getElementById("pcp-mode-note").textContent = hedgeNote;

  const pnlAt = (S, t) => dir * (
    warrantShares * warrantPnLPerShare(S, row, t) +
    optionShares  * optionPnLPerShare(S, row, t)
  );

  // X-range must always contain the spot AND both strikes, with padding
  // on each side — otherwise deep-ITM/OTM pairs push the payoff kinks
  // (which sit at the strikes) off-screen and the curve looks flat.
  const Ko = row.opt_strike, Kw = row.warrant_strike;
  const anchorLo = Math.min(S0, Ko, Kw);
  const anchorHi = Math.max(S0, Ko, Kw);
  const pad = Math.max((anchorHi - anchorLo) * 0.25, S0 * 0.15);
  const lo = Math.max(0, anchorLo - pad), hi = anchorHi + pad;

  const spots = [];
  const pnls  = [];
  const n = 200;
  for (let i=0; i<=n; i++) {
    const S = lo + (hi-lo)*i/n;
    spots.push(parseFloat(S.toFixed(2)));
    pnls.push(parseFloat(pnlAt(S, tau).toFixed(0)));
  }

  // "At expiry" = TRUE terminal payoff of both legs held to their own
  // expiries and settled at intrinsic (exercise iff ITM). No Black-Scholes
  // / time value — even when the warrant outlives the option, it is valued
  // at its final intrinsic, not a BS mark with residual days.
  const isCallLeg = row.warrant_type === "Call";
  const wEntryExp = row.warrant_per_share;      // paid/received per warrant share
  const oEntryExp = row.synthetic_price;        // option entry per share (= opt_per_share)
  const intrinsic = (S, K) => isCallLeg ? Math.max(0, S - K) : Math.max(0, K - S);
  const pnlAtExpiry = (S) => dir * (
    warrantShares * (intrinsic(S, Kw) - wEntryExp) +
    optionShares  * (oEntryExp - intrinsic(S, Ko))
  );
  const pnlsExp = spots.map(S => parseFloat(pnlAtExpiry(S).toFixed(0)));

  const zeroLine = [{x:[lo,hi],y:[0,0],mode:"lines",
    line:{color:"rgba(255,255,255,0.15)",dash:"dash",width:1},
    showlegend:false,hoverinfo:"skip"}];

  // Only the terminal (at-expiry) payoff is shown — a solid green line.
  // The Black-Scholes DTE curve is intentionally gone: it required an IV
  // the arb scan no longer computes and only muddied the real payoff.
  const expiryTrace = {
    x:spots, y:pnlsExp, mode:"lines", name:"P&L at expiry",
    line:{color:"#4ade80",width:2.5},
    hovertemplate:"Spot: %{x:,.0f}<br>P&L at expiry: %{y:+,.0f} NT$<extra></extra>",
  };

  // Mark current spot on the at-expiry curve
  const spotPnl = parseFloat(pnlAtExpiry(S0).toFixed(0));
  const spotTrace = {
    x:[S0], y:[spotPnl], mode:"markers+text", name:"Current spot",
    marker:{color:"white",size:7,symbol:"circle"},
    text:[`${spotPnl>=0?"+":""}${spotPnl.toLocaleString()}`],
    textposition:"top center", textfont:{color:"white",size:11},
    hovertemplate:"Current spot: %{x:,.0f}<br>P&L: %{y:+,.0f} NT$<extra></extra>",
  };

  // dir > 0 means the warrant is the long leg and the option the short one.
  const _mk = payoffStrikeMarks([
    { K: Kw, label: `${dir > 0 ? "Long" : "Short"} W`, dir: dir },
    { K: Ko, label: `${dir > 0 ? "Short" : "Long"} O`, dir: -dir },
  ], S0);

  Plotly.react("pcp-pnl-chart", [...zeroLine, expiryTrace, spotTrace], {
    shapes: _mk.shapes, annotations: _mk.annotations,
    paper_bgcolor:"#13161f", plot_bgcolor:"#0d0f16",
    font:{color:"#8b90a0",family:"Inter,system-ui,sans-serif",size:12},
    xaxis:{title:`Spot Price (NT$) — ${row.warrant_name}`,gridcolor:"#252836",zerolinecolor:"#252836",tickformat:","},
    yaxis:{title:_pcpChartMode==="whole"?`P&L (NT$, whole-張 hedge)`:`P&L (NT$, exact ${contractSize.toLocaleString()}-share hedge)`,gridcolor:"#252836",zerolinecolor:"#252836",tickformat:"+,"},
    legend:{bgcolor:"#13161f",bordercolor:"#252836",borderwidth:1},
    margin:{l:80,r:20,t:20,b:60},
    hovermode:"x unified",
  },{responsive:true});
}

// ── Portfolio ──────────────────────────────────────────────────────
