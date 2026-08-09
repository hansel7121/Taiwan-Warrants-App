// Black-Scholes primitives + payoff-chart furniture, shared by every mode (no DOM).
// Split out of quant.js so the user Dashboard can mark legs and draw payoff
// curves without loading the arb-specific EV math that stayed behind there.

const R_FREE = 0.01875;

function normCDF(x) {
  const a1=0.254829592,a2=-0.284496736,a3=1.421413741,a4=-1.453152027,a5=1.061405429,p=0.3275911;
  const sign = x < 0 ? -1 : 1;
  const t = 1 / (1 + p * Math.abs(x) / Math.SQRT2);
  const y = 1 - ((((a5*t+a4)*t+a3)*t+a2)*t+a1)*t*Math.exp(-x*x/2);
  return 0.5*(1+sign*y);
}

function bsCall(S,K,T,r,sigma) {
  if(T<=0) return Math.max(0,S-K);
  const d1=(Math.log(S/K)+(r+.5*sigma*sigma)*T)/(sigma*Math.sqrt(T));
  return S*normCDF(d1)-K*Math.exp(-r*T)*normCDF(d1-sigma*Math.sqrt(T));
}

function bsPut(S,K,T,r,sigma) {
  if(T<=0) return Math.max(0,K-S);
  const d1=(Math.log(S/K)+(r+.5*sigma*sigma)*T)/(sigma*Math.sqrt(T));
  return K*Math.exp(-r*T)*normCDF(-(d1-sigma*Math.sqrt(T)))-S*normCDF(-d1);
}

function normPDF(x){ return Math.exp(-x*x/2)/Math.sqrt(2*Math.PI); }
// Per-underlying-share BS greeks. Delta is dimensionless (∂price/∂S);
// vega is per 1.00 vol (÷100 at display for per-vol-point). Guard the
// degenerate T/σ=0 case with the intrinsic delta and zero vega.

function bsDelta(S,K,T,r,sigma,isPut){
  if(T<=0||sigma<=0){ const itm = isPut ? S<K : S>K; return isPut ? (itm?-1:0) : (itm?1:0); }
  const d1=(Math.log(S/K)+(r+.5*sigma*sigma)*T)/(sigma*Math.sqrt(T));
  return isPut ? normCDF(d1)-1 : normCDF(d1);
}

function bsVega(S,K,T,r,sigma){
  if(T<=0||sigma<=0) return 0;
  const d1=(Math.log(S/K)+(r+.5*sigma*sigma)*T)/(sigma*Math.sqrt(T));
  return S*normPDF(d1)*Math.sqrt(T);   // per 1.00 vol
}

// ── Shared payoff-chart furniture ────────────────────────────────────
// A payoff curve only bends at a strike, so the strikes are the whole risk
// picture. Draw one dotted vertical per leg — green for a leg you own (dir > 0),
// red for one you owe (dir < 0) — plus a solid line at spot, and label each.
// Returns Plotly {shapes, annotations} to merge into a layout (no DOM).
//   marks: [{ K, label, dir }]   dir > 0 = long leg, dir < 0 = short leg.
const MARK_LONG = "#4ade80", MARK_SHORT = "#f87171";
function payoffStrikeMarks(marks, spot) {
  const shapes = [], annotations = [];
  if (spot != null && isFinite(spot)) {
    shapes.push({ type: "line", x0: spot, x1: spot, yref: "paper", y0: 0, y1: 1,
      line: { color: "rgba(255,255,255,0.35)", width: 1 } });
    annotations.push({ x: spot, yref: "paper", y: 1, yanchor: "bottom",
      text: `spot ${Math.round(spot).toLocaleString()}`, showarrow: false,
      font: { size: 9, color: "#c8ccd8" } });
  }
  (marks || []).filter(m => m && m.K != null && isFinite(m.K)).forEach((m, i) => {
    const col = m.dir > 0 ? MARK_LONG : MARK_SHORT;
    shapes.push({ type: "line", x0: m.K, x1: m.K, yref: "paper", y0: 0, y1: 1,
      line: { color: col, width: 1, dash: "dot" } });
    // Alternate the label height so near-identical strikes don't collide.
    annotations.push({ x: m.K, yref: "paper", y: 1 - (i % 2) * 0.09, yanchor: "top",
      text: `${m.label} ${Number(m.K).toLocaleString(undefined, { maximumFractionDigits: 2 })}`,
      showarrow: false, font: { size: 9, color: col },
      bgcolor: "rgba(13,15,22,0.85)", borderpad: 2 });
  });
  return { shapes, annotations };
}
