// Static-arb structure breakdown modal: leg-by-leg cash flows plus the
// payoff-at-horizon curve for a multi-leg structure from logic/static_arb.py.
// Opened from Portfolio → Suggestions (portfolio.js) via openStaticArbModal().

const _sn = (v, d = 2) => v == null ? "—"
  : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });

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
