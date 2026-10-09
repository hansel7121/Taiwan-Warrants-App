// Arb Finder → StatArb sub-tab: draws the seven TSMC simulation charts from /statarb_state (logic/statarb_logic.py).
// The Update button POSTs /statarb_update, which refetches 2330 closes and rebuilds everything server-side.

let _saLoaded = false;
let _saData = null;

const SA_BLUE = "#4c9be8", SA_RED = "#e06c6c", SA_GREY = "rgba(160,170,180,0.55)";

function statarbLoadOnce() {
  if (_saLoaded) { _saResize(); return; }
  _saLoaded = true;
  _saFetch("/statarb_state", undefined, "Loading (first build downloads prices and simulates; ~10 s)…");
}

function statarbUpdate() {
  _saFetch("/statarb_update", { method: "POST" }, "Fetching latest prices and re-simulating…");
}

async function _saFetch(url, opts, msg) {
  const status = document.getElementById("sa-status"), btn = document.getElementById("sa-update-btn");
  status.textContent = msg;
  btn.disabled = true;
  try {
    _saData = await apiJson(url, opts);
    _saRender(_saData);
  } catch (e) {
    status.textContent = "failed: " + (e.message || e);
    _saLoaded = !!_saData;
  } finally {
    btn.disabled = false;
  }
}

function _saResize() {
  document.querySelectorAll("#arb-sub-statarb .sa-plot").forEach(el => { if (el.data) Plotly.Plots.resize(el); });
}

function _saLayout(extra) {
  const base = {
    paper_bgcolor: "#151a1f", plot_bgcolor: "#0d1013",
    font: { color: "#7b8794", family: "ui-monospace, SFMono-Regular, Menlo, monospace", size: 11 },
    margin: { l: 60, r: 15, t: 30, b: 45 },
    legend: { bgcolor: "rgba(0,0,0,0)", font: { size: 10 } },
  };
  const ax = { gridcolor: "#222a31", zerolinecolor: "#222a31" };
  const out = Object.assign(base, extra);
  out.xaxis = Object.assign({}, ax, extra.xaxis || {});
  out.yaxis = Object.assign({}, ax, extra.yaxis || {});
  return out;
}

function _saPlot(id, traces, layout) {
  Plotly.react(id, traces, _saLayout(layout), { responsive: true, displayModeBar: false });
}

function _saPct(v, d) { return v === null || v === undefined ? "—" : (v * 100).toFixed(d === undefined ? 1 : d) + "%"; }

// Two fan charts side by side on one shared price axis; specs are [id, fan, title, optional reference median].
function _saFanPair(d, specs) {
  const lo = Math.min(...specs.map(s => Math.min(...s[1].bands.p1)));
  const hi = Math.max(...specs.map(s => Math.max(...s[1].bands.p99)));
  specs.forEach(([id, m, title, refMedian]) => {
    const x = d.fan.days, b = m.bands;
    const band = (lo, hi, op, name) => [
      { x, y: b[lo], mode: "lines", line: { width: 0 }, showlegend: false, hoverinfo: "skip" },
      { x, y: b[hi], mode: "lines", line: { width: 0 }, fill: "tonexty", fillcolor: `rgba(76,155,232,${op})`, name, hoverinfo: "skip" },
    ];
    const ref = refMedian ? [{ x, y: refMedian, mode: "lines", name: "zero-drift median", line: { color: SA_RED, dash: "dash", width: 1.5 },
                               hovertemplate: "day %{x}<br>zero-drift median %{y:,.0f}<extra></extra>" }] : [];
    _saPlot(id, [
      ...band("p1", "p99", 0.15, "1-99%"), ...band("p5", "p95", 0.25, "5-95%"), ...band("p25", "p75", 0.4, "25-75%"),
      ...m.samples.map(p => ({ x, y: p, mode: "lines", line: { color: SA_GREY, width: 0.6 }, showlegend: false, hoverinfo: "skip" })),
      { x, y: b.p50, mode: "lines", name: "median", line: { color: SA_BLUE, width: 2 }, hovertemplate: "day %{x}<br>median %{y:,.0f}<extra></extra>" },
      ...ref,
      { x: [x[0], x[x.length - 1]], y: [d.s0, d.s0], mode: "lines", line: { color: "#e6e6e6", dash: "dot", width: 1 }, showlegend: false, hoverinfo: "skip" },
    ], { title: { text: title, font: { size: 12 } }, xaxis: { title: "trading days ahead" },
         yaxis: { title: "TWD", range: [lo * 0.97, hi * 1.03] }, legend: { x: 0.01, y: 1 } });
  });
}

function _saRender(d) {
  const k = d.kpis;
  document.getElementById("sa-status").textContent =
    `${d.ticker} · data as of ${d.asof} · computed ${d.computed_at.replace("T", " ").slice(0, 16)} TPE`;
  const kpi = (l, v, s) => `<div class="sa-kpi"><div class="sa-kpi-l">${l}</div><div class="sa-kpi-v">${v}</div><div class="sa-kpi-s">${s || ""}</div></div>`;
  document.getElementById("sa-kpis").innerHTML = [
    kpi("Spot S0", d.s0.toLocaleString(undefined, { maximumFractionDigits: 1 }), "dividend-adjusted close"),
    kpi("Vol today (EWMA)", _saPct(k.sig_today_ann), `${k.pct_today.toFixed(0)}th percentile of ${d.params.lookback_years}y`),
    kpi("Long-run vol (GARCH)", _saPct(k.sig_lr_ann), k.garch_fallback ? "fallback: sample vol" : `${d.params.lookback_years}y sample ${_saPct(k.sig_sample_ann)}`),
    kpi("Half-life", `${k.half_life} days`, `persistence φ = ${k.phi}`),
    kpi("Shock kurtosis", k.z_kurt.toFixed(2), `normal = 3 · skew ${k.z_skew.toFixed(2)}`),
    kpi("|z| > 3 days", `${k.z_tail_days}`, `vs ${k.z_tail_normal} under normal`),
    kpi("Historical drift", _saPct(k.drift_ann), "per year; removed from shocks, added back in chart 6"),
  ].join("");

  // 1. EWMA vol vs |daily return|
  _saPlot("sa-ewma", [
    { x: d.ewma.dates, y: d.ewma.abs_ann, mode: "lines", name: "|daily return| (annualised)",
      line: { color: "rgba(76,155,232,0.45)", width: 0.6 }, hovertemplate: "%{x}<br>%{y:.1%}<extra></extra>" },
    { x: d.ewma.dates, y: d.ewma.ewma_ann, mode: "lines", name: "EWMA vol (annualised)",
      line: { color: SA_RED, width: 1.8 }, hovertemplate: "%{x}<br>EWMA %{y:.1%}<extra></extra>" },
  ], { xaxis: { type: "date" }, yaxis: { range: [0, 1.2], tickformat: ".0%" }, legend: { x: 1, xanchor: "right", y: 1 } });

  // 2. z histogram (log) + QQ plot
  _saPlot("sa-zhist", [
    { x: d.zhist.centers, y: d.zhist.density, type: "bar", name: "z (bootstrap pool)",
      marker: { color: SA_BLUE, opacity: 0.75 }, hovertemplate: "z %{x:.2f}<br>%{y:.4f}<extra></extra>" },
    { x: d.zhist.normal_x, y: d.zhist.normal_pdf, mode: "lines", name: "standard normal",
      line: { color: "#e6e6e6", dash: "dash", width: 1.4 }, hoverinfo: "skip" },
  ], { title: { text: "Histogram (log scale shows the tails)", font: { size: 12 } }, bargap: 0,
       xaxis: { title: "standardised shock z (units of that day's vol)", range: [-6.5, 7] },
       yaxis: { type: "log", range: [-4, 0], title: "density (log)", exponentformat: "power" },
       legend: { x: 1, xanchor: "right", y: 1 } });
  const qx = [d.qq.theo[0], d.qq.theo[d.qq.theo.length - 1]];
  _saPlot("sa-qq", [
    { x: d.qq.theo, y: d.qq.ordered, mode: "markers", name: "z", marker: { color: SA_BLUE, size: 5 },
      hovertemplate: "theoretical %{x:.2f}<br>actual %{y:.2f}<extra></extra>" },
    { x: qx, y: qx.map(v => d.qq.intercept + d.qq.slope * v), mode: "lines", name: "normal fit",
      line: { color: SA_RED, width: 1.5 }, hoverinfo: "skip" },
  ], { title: { text: "QQ plot vs normal: points off the line at the ends = fat tails", font: { size: 12 } },
       showlegend: false, xaxis: { title: "theoretical quantiles" }, yaxis: { title: "ordered values" } });

  // 3. EWMA vol distribution
  const v = d.volhist;
  _saPlot("sa-volhist", [
    { x: v.values, type: "histogram", nbinsx: 60, name: "EWMA vol", marker: { color: SA_BLUE, opacity: 0.75 },
      hovertemplate: "%{x}<br>%{y} days<extra></extra>" },
    { x: [v.today, v.today], y: [0, 1], yaxis: "y2", mode: "lines", name: `today: ${_saPct(v.today)}`,
      line: { color: SA_RED, width: 2.2 }, hoverinfo: "skip" },
    { x: [v.sample, v.sample], y: [0, 1], yaxis: "y2", mode: "lines", name: `${d.params.lookback_years}y average: ${_saPct(v.sample)}`,
      line: { color: "#e6e6e6", width: 1.6, dash: "dash" }, hoverinfo: "skip" },
  ], { bargap: 0, xaxis: { tickformat: ".0%" }, yaxis: { title: "days" },
       yaxis2: { overlaying: "y", range: [0, 1], visible: false }, legend: { x: 1, xanchor: "right", y: 1 } });

  // 4. forecast vol path + average vol over horizon
  const f = d.forecast, last = f.days[f.days.length - 1];
  _saPlot("sa-volpath", [
    { x: f.days, y: f.sig_path_ann, mode: "lines", name: "forecast daily vol", line: { color: SA_BLUE, width: 2 },
      hovertemplate: "day %{x}<br>%{y:.1%}<extra></extra>" },
    { x: [0, last], y: [f.today_ann, f.today_ann], mode: "lines", name: "flat sigma_today",
      line: { color: SA_RED, dash: "dot", width: 1.5 }, hoverinfo: "skip" },
    { x: [0, last], y: [f.lr_ann, f.lr_ann], mode: "lines", name: "long-run",
      line: { color: "#e6e6e6", dash: "dash", width: 1.5 }, hoverinfo: "skip" },
  ], { title: { text: "Vol for each future day (annualised)", font: { size: 12 } },
       xaxis: { title: "trading day ahead" }, yaxis: { tickformat: ".1%" }, legend: { x: 1, xanchor: "right", y: 0.4 } });
  _saPlot("sa-avgvol", [
    { x: f.days, y: f.avg_ann, mode: "lines", name: "average vol", line: { color: SA_BLUE, width: 2 },
      hovertemplate: "N = %{x}<br>%{y:.1%}<extra></extra>" },
  ], { title: { text: "Average vol over the holding period (annualised)", font: { size: 12 } }, showlegend: false,
       xaxis: { title: "holding horizon N (days)" }, yaxis: { tickformat: ".1%" } });

  // 5. fan charts: bootstrap vs GBM (zero drift)
  const paths = `${d.params.n_sims.toLocaleString()} paths each`;
  document.getElementById("sa-fan-h").textContent =
    `5. Simulated ${d.ticker.replace(".TW", "")} price paths from S0 = ${Math.round(d.s0).toLocaleString()} (${paths})`;
  _saFanPair(d, [["sa-fan-boot", d.fan.boot, "Filtered block bootstrap"], ["sa-fan-gbm", d.fan.gbm, "GBM fed the vol path"]]);

  // 6. fan charts: bootstrap with zero drift vs historical drift
  document.getElementById("sa-drift-h").textContent =
    `6. Bootstrap paths: zero drift vs historical drift (${_saPct(k.drift_ann)}/yr, ${d.params.lookback_years}y average) · ${paths}`;
  _saFanPair(d, [["sa-fan-zero", d.fan.boot, "Zero drift"],
                 ["sa-fan-drift", d.fan.boot_drift, `Historical drift (${_saPct(k.drift_ann)}/yr)`, d.fan.boot.bands.p50]]);

  // 7. two-sample t-test overlays, one per horizon
  const box = document.getElementById("sa-ttest");
  box.innerHTML = d.ttest.map(t => `<div id="sa-tt-${t.h}" class="sa-plot sa-tall"></div>`).join("");
  d.ttest.forEach(t => {
    const s = (m, tag) => `${tag} mean ${m.mean >= 0 ? "+" : ""}${m.mean.toFixed(2)}%, median ${m.median >= 0 ? "+" : ""}${m.median.toFixed(2)}%, std ${m.std.toFixed(2)}%`;
    const note = [s(t.boot, "boot"), s(t.gbm, "GBM "), `Δ mean (boot − GBM) = ${t.dmean >= 0 ? "+" : ""}${t.dmean.toFixed(3)}%`,
                  `Welch t = ${t.t.toFixed(2)}, p = ${t.p.toFixed(4)}`, `Levene (spread) p = ${t.levene_p.toFixed(4)}`].join("<br>");
    const dens = (m, color, name) => [
      { x: m.centers, y: m.density, type: "bar", name, marker: { color, opacity: 0.4 }, hoverinfo: "skip" },
      { x: t.grid, y: m.kde, mode: "lines", line: { color, width: 2 }, showlegend: false,
        hovertemplate: `${name}<br>%{x:.1f}%: %{y:.4f}<extra></extra>` },
      { x: [m.mean, m.mean], y: [0, 1], yaxis: "y2", mode: "lines", line: { color, dash: "dash", width: 1.5 }, showlegend: false, hoverinfo: "skip" },
    ];
    _saPlot(`sa-tt-${t.h}`, [
      ...dens(t.gbm, SA_RED, `GBM fed the vol path (n=${d.params.n_sims.toLocaleString()})`),
      ...dens(t.boot, SA_BLUE, `Filtered block bootstrap (n=${d.params.n_sims.toLocaleString()})`),
    ], { title: { text: `Two-sample t-test: ${t.h}-day log return`, font: { size: 12 } }, barmode: "overlay", bargap: 0,
         xaxis: { title: `${t.h}-day log return (%)` }, yaxis: { title: "density" },
         yaxis2: { overlaying: "y", range: [0, 1], visible: false },
         legend: { x: 1, xanchor: "right", y: 1 }, margin: { l: 60, r: 15, t: 30, b: 45 },
         annotations: [{ xref: "paper", yref: "paper", x: 0.01, y: 0.99, xanchor: "left", yanchor: "top", align: "left",
                         text: note, showarrow: false, font: { size: 10, color: "#c9d1d9" },
                         bgcolor: "rgba(13,16,19,0.85)", bordercolor: "#222a31", borderpad: 5 }] });
  });
}

// 8. Pair scanner: POST /statarb_scan, then filter client-side by the P(loss) slider.
let _saScan = null;
let _saShown = [];
const SA_SCEN = [["zero", "P zero drift"], ["drift", "P drift"], ["stress", "P vol ×1.2"], ["stress_drift", "P vol ×1.2 + drift"]];

async function statarbScan() {
  const status = document.getElementById("sa-scan-status"), btn = document.getElementById("sa-scan-btn");
  status.textContent = "Fetching 2330 warrants and options and scoring every pair…";
  btn.disabled = true;
  try {
    _saScan = await apiJson("/statarb_scan", { method: "POST" });
    status.textContent = `scanned ${_saScan.scanned_at.replace("T", " ").slice(0, 16)} TPE · quotes as of ` +
      `${(_saScan.quotes_as_of || "?").replace("T", " ").slice(0, 16)} UTC · paths as of ${_saScan.sim_asof}`;
    _saScanRender();
  } catch (e) {
    status.textContent = "failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

// Worst P(loss) across the four path sets for the chosen measure; null when the pair can't be tested.
function _saWorst(r, measure) {
  if (r.pure) return 0;
  const p = r[measure];
  return p ? Math.max(...Object.values(p)) : null;
}

function _saScanRender() {
  const thrRaw = +document.getElementById("sa-scan-thr").value;
  const thr = thrRaw / 10000;                       // slider unit = 0.01%
  document.getElementById("sa-scan-thr-v").textContent = (thrRaw / 100).toFixed(2) + "%";
  if (!_saScan) return;
  const measure = document.getElementById("sa-scan-measure").value;
  const showAll = document.getElementById("sa-scan-all").checked;
  const rows = _saScan.rows;
  const passes = r => { const w = _saWorst(r, measure); return w !== null && w <= thr; };
  const pure = rows.filter(r => r.pure), pass = rows.filter(passes);
  const untestable = rows.filter(r => !r.pure && !r.testable);
  const fmtP = p => p === null || p === undefined ? "—" : (p * 100).toFixed(2) + "%";
  const fmtN = v => Math.round(v).toLocaleString();

  document.getElementById("sa-scan-summary").innerHTML =
    `Spot <b>${fmtN(_saScan.spot)}</b> · ${_saScan.n_warrants} warrants, ${_saScan.n_options_live_bid} options with a live bid · ` +
    `<b>${rows.length}</b> pairs with a net credit · pure arb <b>${pure.length}</b> · ` +
    `pass at max P(loss) ${(thrRaw / 100).toFixed(2)}% on all four path sets <b>${pass.length}</b> ` +
    `(fillable at ask depth <b>${pass.filter(r => r.fillable).length}</b>)` +
    (untestable.length ? ` · ${untestable.length} expire beyond ${_saScan.n_max} trading days, not testable` : "") +
    `<br>PnL is measured at the option's expiry with the warrant valued at intrinsic only, one option contract against ` +
    `whole board lots of warrants, before fees and tax. ${_saScan.n_sims.toLocaleString()} paths per set.`;

  const shown = showAll ? rows : pass;
  _saShown = shown;
  const head = ["Status", "Warrant", "Name", "Option", "Type", "K warrant", "K option", "W DTE", "O DTE", "Trading days",
                "Lots", "Fillable", "Credit (TWD)", "Max loss (TWD)", "Loss region", "Dist. to loss",
                ...SA_SCEN.map(s => s[1]), "Worst"];
  const region = r => r.loss_region.map(([a, b]) => `${fmtN(a)}–${b === null ? "∞" : fmtN(b)}`).join(", ") || "none";
  const body = shown.map((r, i) => {
    const worst = _saWorst(r, measure), ok = passes(r);
    const p = r[measure] || {};
    return `<tr onclick="_saOpenPair(${i})" title="Click for the trade legs and payoff at expiry"><td class="${ok ? "sa-pass" : "sa-fail"}">${r.pure ? "PURE" : ok ? "PASS" : r.testable ? "fail" : "untestable"}</td>` +
      `<td>${escHtml(r.warrant_code)}</td><td>${escHtml(r.warrant_name)}</td><td>${escHtml(r.option_contract)}</td>` +
      `<td>${r.type}</td><td>${r.warrant_strike.toLocaleString()}</td><td>${r.opt_strike.toLocaleString()}</td>` +
      `<td>${r.warrant_dte}</td><td>${r.opt_dte}</td><td>${r.trading_days}</td><td>${r.lots}</td>` +
      `<td>${r.fillable ? "yes" : "no"}</td><td>${fmtN(r.credit)}</td><td>${fmtN(r.max_loss)}</td>` +
      `<td>${region(r)}</td><td>${r.dist_to_loss_pct === null ? "—" : r.dist_to_loss_pct.toFixed(1) + "%"}</td>` +
      SA_SCEN.map(([k]) => `<td>${r.pure ? "0.00%" : fmtP(p[k])}</td>`).join("") +
      `<td>${fmtP(worst)}</td></tr>`;
  }).join("");
  document.getElementById("sa-scan-table").innerHTML = shown.length
    ? `<table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`
    : `<div class="sa-scan-summary" style="padding:12px">No pair passes at this threshold. Raise the slider or tick "show failing pairs too".</div>`;
}

// Open the Direct Match trade modal (legs, depth, payoff at expiry) for scanner row i.
function _saOpenPair(i) {
  const r = _saShown[i], cs = r.opt_contract_size;
  _pcpChartMode = "whole";             // the scanner sizes the warrant leg in whole board lots
  openDirectModal({
    warrant_code: r.warrant_code, warrant_name: r.warrant_name, type: r.type, option_contract: r.option_contract,
    underlying_price: _saScan.spot, warrant_dte: r.warrant_dte, opt_dte: r.opt_dte,
    dte_diff: Math.abs(r.warrant_dte - r.opt_dte), warrant_strike: r.warrant_strike, opt_strike: r.opt_strike,
    strike_diff_pct: Math.abs(r.opt_strike - r.warrant_strike) / r.warrant_strike * 100,
    warrants_needed: Math.round(cs / r.exercise_ratio), opt_contract_size: cs,
    warrant_depth_lots: r.warrant_depth_lots, fillable: r.fillable,
    warrant_ask: r.warrant_ask, warrant_bid: r.warrant_bid, opt_bid: r.opt_bid, opt_ask: r.opt_ask,
    warrant_per_share: r.warrant_ask / r.exercise_ratio, opt_per_share: r.opt_bid,
    price_diff: r.price_diff, price_diff_pct: r.price_diff / r.opt_bid * 100,
    warrant_iv: null, opt_iv: null,
  });
}
