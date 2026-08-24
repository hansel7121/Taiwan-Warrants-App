// Arb Finder -> static-arbitrage LP sub-tabs.
// Structures here carry a VARIABLE number of legs, so none of arb.js's fixed
// two/three-leg renderers apply — this file owns its own table, modal and
// payoff-at-horizon chart. Talks to /match_static_arb (logic/static_arb.py).
//
// One sub-tab, "experiment": the LP buys warrants and options and sells options
// only. Warrants are never sold — see logic/static_arb.py. The mode indirection
// stays because every element id here is prefixed per mode (exp*).
const STATIC_ARB_MODES = {
  experiment: { prefix: "exp" },
};

const staticArbData = { experiment: [] };

const _sn = (v, d = 2) => v == null ? "—"
  : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });

const _saEl = (mode, suffix) =>
  document.getElementById(STATIC_ARB_MODES[mode].prefix + suffix);

function getStaticArbFilters(mode) {
  return {
    stock_codes: selectedCodes(_saEl(mode, "StockSelect")),
    min_volume: parseInt(_saEl(mode, "MinVolume").value) || 0,
    min_edge: parseFloat(_saEl(mode, "MinEdge").value) || 0,
    max_horizon_dte: parseInt(_saEl(mode, "MaxHorizon").value) || 0,
  };
}

async function runStaticArb(mode) {
  const status = _saEl(mode, "-status");
  status.textContent = "Solving one LP per underlying × horizon…";
  _saEl(mode, "-tableContainer").innerHTML = "";
  _saEl(mode, "DownloadBtn").style.display = "none";

  let data;
  try {
    const res = await api("/match_static_arb", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getStaticArbFilters(mode)),
    });
    data = await res.json();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    status.textContent = "Error: " + data.error;
    return;
  }

  staticArbData[mode] = data.rows || [];
  // An empty LP result is a PROOF, not a null result: the dual of an infeasible
  // credit is a state-price system consistent with every quote. Say so, and keep
  // the server's note (which reports legs dropped for missing resting size)
  // because "no arb exists" and "MIS was down" must not read the same.
  const head = data.count
    ? `${data.count} static-arb structures — each pays a net credit today and can never pay out at its horizon`
    : "No static arb exists in the scanned chains — the LP is complete over single-horizon static portfolios, so this is a proof, not a miss";
  const dropped = data.dropped_no_depth
    ? ` · ⚠ ${data.dropped_no_depth} legs skipped (no resting size)` : "";
  status.textContent = head + dropped + (data.note ? ` · ${data.note}` : "") + asOfLabel(data);

  if (data.count) _saEl(mode, "DownloadBtn").style.display = "inline-block";
  renderStaticArbTable(mode);
}

function sortStaticArbBy(mode, field, asc) {
  staticArbData[mode].sort((a, b) => {
    const x = a[field], y = b[field];
    if (x == null) return 1;
    if (y == null) return -1;
    return asc ? x - y : y - x;
  });
  renderStaticArbTable(mode);
}

async function downloadStaticArbCSV(mode) {
  const res = await api("/match_static_arb_csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getStaticArbFilters(mode)),
  });
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "static_arb.csv";
  a.click();
}

// One compact line per leg, coloured by side. Warrants show 張, options 口.
function _legLine(leg) {
  const color = leg.side === "long" ? MARK_LONG : MARK_SHORT;
  const verb = leg.side === "long" ? "Buy" : "Sell";
  return `<div style="color:${color};font-size:11px;white-space:nowrap">
    ${verb} ${_sn(leg.lots, 0)}${leg.lot_label} ${leg.name}
    <span style="color:var(--muted)">· ${leg.type[0]}${_sn(leg.strike)} · ${leg.dte}d @ ${_sn(leg.quote, 4)}</span>
  </div>`;
}

function renderStaticArbTable(mode) {
  const rows = staticArbData[mode];
  const c = _saEl(mode, "-tableContainer");
  const maxLegs = parseInt(_saEl(mode, "MaxLegs").value) || 0;
  const shown = maxLegs > 0 ? rows.filter(r => r.n_legs <= maxLegs) : rows;

  if (!shown.length) {
    c.innerHTML = `<p style='padding:16px;color:var(--muted)'>${
      rows.length ? "Every structure exceeds the Max Legs filter." : "Nothing to show."
    }</p>`;
    return;
  }

  const th = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;border-bottom:1px solid var(--border);text-align:left";
  const td = "padding:7px 10px;border-bottom:1px solid var(--border);font-size:12px;vertical-align:top";
  const sub = "color:var(--muted);font-size:11px";

  let h = `<table style="width:100%;border-collapse:collapse"><thead><tr>
    <th style="${th}">Underlying</th><th style="${th}">Horizon</th>
    <th style="${th}">Legs</th>
    <th style="${th};text-align:right">Guaranteed</th>
    <th style="${th};text-align:right">Credit</th>
  </tr></thead><tbody>`;

  shown.forEach(r => {
    const idx = rows.indexOf(r);
    h += `<tr style="cursor:pointer" onclick="openStaticArbModal(staticArbData['${mode}'][${idx}])" title="Click for the full breakdown and payoff curve">
      <td style="${td}">
        <div style="font-weight:600">${r.underlying_code}</div>
        <div style="${sub}">spot ${_sn(r.underlying_price)}</div>
      </td>
      <td style="${td}">
        <div style="font-weight:600">${r.horizon_dte}d</div>
        <div style="${sub}">${r.n_long}L / ${r.n_short}S</div>
      </td>
      <td style="${td}">${r.legs.map(_legLine).join("")}</td>
      <td style="${td};text-align:right">
        <div class="put" style="font-weight:700">+${_sn(r.guaranteed_profit, 0)}</div>
        <div class="put" style="font-size:11px">${r.return_pct == null ? "" : "+" + _sn(r.return_pct) + "%"}</div>
        <div style="${sub}">worst @ ${_sn(r.worst_spot)}</div>
      </td>
      <td style="${td};text-align:right">
        <div style="font-weight:600">${_sn(r.net_credit, 0)}</div>
        <div style="${sub}">debit ${_sn(r.gross_debit, 0)}</div>
      </td></tr>`;
  });
  c.innerHTML = h + "</tbody></table>";
}

// ── Modal: leg-by-leg cash flows + the payoff floor across spot ──────────────

function closeStaticArbModal() {
  document.getElementById("staticArbModal").style.display = "none";
  Plotly.purge("static-arb-payoff-chart");
}

// Payoff at the horizon: shorts settle exactly, longs are marked at the European
// lower bound the LP used (kink at the DISCOUNTED strike, not the nominal one).
function _staticArbPayoff(row, S) {
  let total = 0;
  row.legs.forEach(leg => {
    const k = leg.eff_strike;
    const v = leg.type === "Call" ? Math.max(0, S - k) : Math.max(0, k - S);
    total += (leg.side === "long" ? 1 : -1) * leg.shares * v;
  });
  return total;
}

function openStaticArbModal(r) {
  const td = "padding:6px 9px;border-bottom:1px solid var(--border);font-size:12px";
  const th = "padding:6px 9px;color:var(--muted);font-size:11px;text-transform:uppercase;border-bottom:1px solid var(--border);text-align:left";

  let legRows = "";
  r.legs.forEach(leg => {
    const color = leg.side === "long" ? MARK_LONG : MARK_SHORT;
    legRows += `<tr>
      <td style="${td};color:${color};font-weight:600">${leg.side === "long" ? "Buy" : "Sell"}</td>
      <td style="${td}">${leg.name}<div style="color:var(--muted);font-size:11px">${leg.kind}</div></td>
      <td style="${td}">${leg.type} ${_sn(leg.strike)}</td>
      <td style="${td}">${leg.dte}d</td>
      <td style="${td};text-align:right">${_sn(leg.quote, 4)}</td>
      <td style="${td};text-align:right">${_sn(leg.lots, 0)} ${leg.lot_label}</td>
      <td style="${td};text-align:right">${_sn(leg.shares, 0)}</td>
      <td style="${td};text-align:right;color:${leg.cash >= 0 ? MARK_LONG : MARK_SHORT}">${_sn(leg.cash, 0)}</td>
      <td style="${td};text-align:right;color:var(--muted)">${_sn(leg.eff_strike, 2)}</td>
    </tr>`;
  });

  document.getElementById("static-arb-modal-body").innerHTML = `
    <h2 style="font-size:16px;font-weight:600;color:var(--text);margin-bottom:4px">
      ${r.underlying_code} — static arb, ${r.horizon_dte}d horizon
    </h2>
    <p class="dash-hint" style="margin-bottom:14px">
      Net credit ${_sn(r.net_credit, 0)} NT$ today; payoff floor at the horizon is
      ${_sn(r.min_payoff, 0)} NT$, bottoming at spot ${_sn(r.worst_spot)}.
      Guaranteed ${_sn(r.guaranteed_profit, 0)} NT$ on ${_sn(r.gross_debit, 0)} NT$ deployed.
      Long legs are marked at their European lower bound, so the real floor is higher than plotted.
    </p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:18px"><thead><tr>
      <th style="${th}">Side</th><th style="${th}">Instrument</th><th style="${th}">Strike</th>
      <th style="${th}">DTE</th><th style="${th};text-align:right">Price</th>
      <th style="${th};text-align:right">Size</th><th style="${th};text-align:right">Shares</th>
      <th style="${th};text-align:right">Cash</th><th style="${th};text-align:right">Kink</th>
    </tr></thead><tbody>${legRows}</tbody></table>
    <div id="static-arb-payoff-chart" style="height:340px"></div>`;

  document.getElementById("staticArbModal").style.display = "block";

  const S0 = r.underlying_price || Math.max(...r.legs.map(l => l.strike));
  const hi = Math.max(S0 * 2, Math.max(...r.legs.map(l => l.strike)) * 1.3);
  const xs = [], ys = [];
  for (let i = 0; i <= 240; i++) {
    const S = hi * i / 240;
    xs.push(S);
    ys.push(_staticArbPayoff(r, S) + r.net_credit);
  }

  Plotly.newPlot("static-arb-payoff-chart", [{
    x: xs, y: ys, type: "scatter", mode: "lines",
    line: { color: MARK_LONG, width: 2 },
    name: "Total P&L at horizon",
  }], {
    margin: { l: 60, r: 20, t: 30, b: 44 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#9ca3af", size: 11 },
    xaxis: { title: "Spot at horizon", gridcolor: "rgba(148,163,184,0.15)" },
    yaxis: { title: "P&L (NT$)", gridcolor: "rgba(148,163,184,0.15)" },
    shapes: [
      { type: "line", x0: 0, x1: hi, y0: 0, y1: 0,
        line: { color: "rgba(148,163,184,0.5)", width: 1, dash: "dot" } },
      { type: "line", x0: S0, x1: S0, y0: Math.min(...ys), y1: Math.max(...ys),
        line: { color: "rgba(148,163,184,0.35)", width: 1, dash: "dash" } },
    ],
    showlegend: false,
  }, { displayModeBar: false, responsive: true });
}

function openStaticArbInfo(mode) {
  alert(
"Experiment — static-arbitrage LP\n\n" +
"The other tabs hand-code one structure each (vertical, put-call parity, butterfly) and search for instances of it. This one solves for the weights instead.\n\n" +
"For each underlying and each horizon T*, it builds a linear program:\n" +
"  maximise   (proceeds from short legs) − (cost of long legs)\n" +
"  subject to portfolio payoff ≥ 0 at every spot, and depth limits per leg\n\n" +
"WARRANTS ARE BUY-ONLY. A long basket may hold warrants and options; the short basket is options ONLY. Warrants get buy variables and nothing else, so the constraint is structural — this tab cannot express a short warrant at all, and every 'Buy Option / Sell Warrant' arb is outside its variable set by design. Selling a warrant means shorting an AMERICAN claim the holder may exercise before T*, which breaks the one-period proof; that direction is Direct Match's, not the LP's. Horizons are therefore the option expiries only.\n\n" +
"Short option legs are TW options, which are European and cash-settled, so they carry no early-assignment risk.\n\n" +
"COMPLETENESS. Every leg payoff is piecewise-linear with one kink, so the portfolio bends only at the leg strikes. A piecewise-linear function is ≥ 0 everywhere if and only if it is ≥ 0 at spot 0, at every kink, and its far-right slope is ≥ 0 — a finite check that is exactly equivalent, not a sampled grid. Within static single-horizon portfolios of quoted instruments the LP therefore finds every arb that exists, and an empty result is a proof that none does. Direct Match, PCP and Butterfly are all weight-restricted special cases.\n\n" +
"HORIZON. Short legs must expire exactly at T*; one that settled earlier settled against a different spot, which a one-period model cannot represent. Long legs may expire at or after T* and are replaced by their European lower bound there — (S − K·d)+ for calls, (K·d − S)+ for puts. Never intrinsic for puts: a European put can trade below intrinsic, and flooring one at (K − S)+ would certify arbs that do not exist.\n\n" +
"SIZING. LP weights are fractional; shorts are rounded DOWN to whole contracts and longs UP to whole 張 / contracts. That direction only lifts the payoff curve, so the floor survives rounding; the credit is then rechecked.\n\n" +
"KNOWN GAP — ROUNDING CAN HIDE A REAL ARB. Each (underlying, horizon) emits only its single best structure, and the rounding above is applied once with no fallback. When rounding turns that winner's credit negative the whole horizon emits NOTHING, even where a smaller structure would still have cleared. So an empty horizon is not a proof in the way an empty LP is: rows shown are sound, but rows are missing. Keep Direct Match running alongside until this is fixed.\n\n" +
"LIMITS. Sound but incomplete — discarding each long leg's remaining time value means real arbs that need it are missed, never invented. Fees and tax are NOT modelled; use Min Edge to cover them. And there is no dividend model: a cash dividend between the horizon and a long leg's expiry lowers that leg's floor, so rows spanning an ex-div date are unverified."
  );
}
