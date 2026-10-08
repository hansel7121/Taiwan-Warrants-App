// Arb Finder → StatArb sub-tab: draws the six TSMC simulation charts from /statarb_state (logic/statarb_logic.py).
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
    kpi("Drift removed", _saPct(k.drift_ann), "per year, before simulating"),
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

  // 5. fan charts
  document.getElementById("sa-fan-h").textContent =
    `5. Simulated ${d.ticker.replace(".TW", "")} price paths from S0 = ${Math.round(d.s0).toLocaleString()} (${d.params.n_sims.toLocaleString()} paths each)`;
  const lo = Math.min(d.fan.boot.bands.p1.reduce((a, b) => Math.min(a, b)), d.fan.gbm.bands.p1.reduce((a, b) => Math.min(a, b)));
  const hi = Math.max(d.fan.boot.bands.p99.reduce((a, b) => Math.max(a, b)), d.fan.gbm.bands.p99.reduce((a, b) => Math.max(a, b)));
  [["sa-fan-boot", d.fan.boot, "Filtered block bootstrap"], ["sa-fan-gbm", d.fan.gbm, "GBM fed the vol path"]].forEach(([id, m, title]) => {
    const x = d.fan.days, b = m.bands;
    const band = (lo, hi, op, name) => [
      { x, y: b[lo], mode: "lines", line: { width: 0 }, showlegend: false, hoverinfo: "skip" },
      { x, y: b[hi], mode: "lines", line: { width: 0 }, fill: "tonexty", fillcolor: `rgba(76,155,232,${op})`, name, hoverinfo: "skip" },
    ];
    _saPlot(id, [
      ...band("p1", "p99", 0.15, "1-99%"), ...band("p5", "p95", 0.25, "5-95%"), ...band("p25", "p75", 0.4, "25-75%"),
      ...m.samples.map(p => ({ x, y: p, mode: "lines", line: { color: SA_GREY, width: 0.6 }, showlegend: false, hoverinfo: "skip" })),
      { x, y: b.p50, mode: "lines", name: "median", line: { color: SA_BLUE, width: 2 }, hovertemplate: "day %{x}<br>median %{y:,.0f}<extra></extra>" },
      { x: [x[0], x[x.length - 1]], y: [d.s0, d.s0], mode: "lines", line: { color: "#e6e6e6", dash: "dot", width: 1 }, showlegend: false, hoverinfo: "skip" },
    ], { title: { text: title, font: { size: 12 } }, xaxis: { title: "trading days ahead" },
         yaxis: { title: "TWD", range: [lo * 0.97, hi * 1.03] }, legend: { x: 0.01, y: 1 } });
  });

  // 6. two-sample t-test overlays, one per horizon
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
