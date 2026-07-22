// Portfolio tab: positions, payoff/P&L charts, auto-close, and page boot.

let portfolioData = [];

let _pfSub = "pnl";

function switchPfSub(sub, btn) {
  _pfSub = sub;
  document.querySelectorAll(".pfsub-content").forEach(el => el.style.display = "none");
  document.querySelectorAll(".pfsub-btn").forEach(el => el.classList.remove("active"));
  document.getElementById("pfsub-" + sub).style.display = "block";
  btn.classList.add("active");
  if (sub === "pnl") renderPnlChart();
}

// The portfolio is loaded lazily on first visit to the Portfolio tab, not
// at page boot — the landing (Options Scanner) tab never reads portfolioData,
// so seeding it on reload just wasted a /get_portfolio plus one
// /adr_premium_scenario POST per underlying (refreshBasis). The Refresh
// button still calls loadPortfolio() directly to force a re-fetch.

let _pfLoaded = false;

function loadPortfolioOnce() {
  if (_pfLoaded) return;
  _pfLoaded = true;
  loadPortfolio();
}

async function loadPortfolio() {
  try {
    const res = await api("/get_portfolio");
    portfolioData = await res.json();
  } catch (e) { portfolioData = []; }
  await autoCloseTrades();
  renderPortfolio();          // paints rows (P&L-now shows "…" until basis lands)
  renderPnlChart();
  _basisCache = {};
  await refreshBasis();       // fetch live ADR premium/FX per underlying
  renderPortfolio();          // repaint with the live P&L-at-expiry-now column
}

// Live ADR premium + FX per underlying, for the per-row "P&L at expiry
// now" column. Fetched once per us_stock_code (endpoint is cached server-side).

let _basisCache = {};

async function refreshBasis() {
  const codes = [...new Set(portfolioData
    .filter(t => !t.closed && (t.mode === "us" || t.mode === "twus" || t.mode === "uspcp") && t.us_stock_code)
    .map(t => t.us_stock_code))];
  await Promise.all(codes.map(async code => {
    try {
      const res = await api("/adr_premium_scenario", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stock_code: code, horizon_days: 30 }),
      });
      const s = await res.json();
      _basisCache[code] = s.error ? null : { p: s.current_premium, fx: (s.fx || {}).current_fx };
    } catch (e) { _basisCache[code] = null; }
  }));
}

// P&L at expiry at the current spot — the exact value of the payoff
// graph's current-spot marker (_payoffAtSpot: both legs settle at
// intrinsic, spot held here). Uses the live ADR premium / FX for us/twus,
// entry basis for direct. Closed -> booked realized P&L. null while the
// live basis is still loading.

function _pnlAtExpiryNow(t) {
  const r = t.row; if (!r || r.underlying_price == null) return null;
  if (t.closed) return t.closed.realized_pnl;
  const mult = t.mult || 1;
  const p0 = t.entry_premium ?? 0, fx0 = t.entry_fx ?? 1;
  if ((t.mode === "us" || t.mode === "twus" || t.mode === "uspcp") && t.us_stock_code) {
    const b = _basisCache[t.us_stock_code];
    if (!b || b.p == null || b.fx == null) return null;   // basis pending/unavailable
    // Intrinsic-only floor: both legs valued at intrinsic at the current
    // spot. The surviving (long) leg's time value is ignored, so this is a
    // conservative lower bound on P&L. Matches the popup's bottom line.
    const rN = (fx0 && b.fx) ? b.fx / fx0 : 1;
    return _payoffAtSpot(r, r.underlying_price, _stripFx(b.p, rN), p0, rN, mult);
  }
  // Direct / no ADR basis: at-expiry payoff at entry basis, current spot.
  return _payoffAtSpot(r, r.underlying_price, p0, p0, 1, mult);
}

// Terminal (at-expiry) P&L of the whole position for a given underlying
// spot S, at premium p / FX ratio — mirrors _evPnlAtPremium but both legs
// settle at intrinsic and S is swept. The ADR premium shifts the option
// leg's spot (S*(1+(p-p0))) and FX scales its TWD value. Entry-basis curve
// uses p=p0, fxRatio=1; current-basis curve uses live premium/FX.

function _payoffAtSpot(row, S, p, p0, fxRatio, mult) {
  const isCall = row.warrant_type === "Call";
  const intr = (x, K) => isCall ? Math.max(0, x - K) : Math.max(0, K - x);
  const shares = row.opt_contract_size;
  const dir = row.pcp_diff >= 0 ? 1 : -1;
  const wEntry = row.warrant_per_share, oEntry = row.synthetic_price;
  const Kw = row.warrant_strike, Ko = row.opt_strike;
  // `p` is the FX-STRIPPED basis premium (moneyness), fxRatio translates
  // the USD leg. Short-option TWD P&L = oEntry − fxRatio·oExit (entry
  // premium fixed in TWD; only the exit leg re-translates) — matches
  // _evPnlAtPremium and avoids double-counting FX.
  const optSpot = S * (1 + (p - p0));
  const perShare = dir * ((intr(S, Kw) - wEntry) + (oEntry - fxRatio * intr(optSpot, Ko)));
  return perShare * shares * (mult || 1);
}

// Payoff-curve chart in the trade-detail popup: P&L at expiry across spot,
// one line at entry basis (ADR & FX unmoved) and one at the current basis.

function renderPfPayoff(t, curP, curFx) {
  const r = t.row;
  const wrap = document.getElementById("pf-payoff-wrap");
  const chart = document.getElementById("pf-payoff-chart");
  if (!r || r.warrant_strike == null || r.opt_strike == null || r.underlying_price == null) {
    wrap.style.display = "none"; Plotly.purge(chart); return;
  }
  const mult = t.mult || 1;
  const p0 = t.entry_premium ?? 0, fx0 = t.entry_fx ?? 1;
  const S0 = r.underlying_price, Kw = r.warrant_strike, Ko = r.opt_strike;
  const lo0 = Math.min(S0, Kw, Ko), hi0 = Math.max(S0, Kw, Ko);
  const pad = Math.max((hi0 - lo0) * 0.25, S0 * 0.15);
  const lo = Math.max(0, lo0 - pad), hi = hi0 + pad, n = 160;
  const haveNow = curP != null && curFx != null;
  const fxRatioNow = (fx0 && curFx) ? curFx / fx0 : 1;
  const spots = [], entry = [], now = [];
  for (let i = 0; i <= n; i++) {
    const S = lo + (hi - lo) * i / n;
    spots.push(parseFloat(S.toFixed(2)));
    entry.push(Math.round(_payoffAtSpot(r, S, p0, p0, 1, mult)));
    if (haveNow) now.push(Math.round(_payoffAtSpot(r, S, _stripFx(curP, fxRatioNow), p0, fxRatioNow, mult)));
  }
  const traces = [{ x: [lo, hi], y: [0, 0], mode: "lines",
    line: { color: "rgba(255,255,255,0.15)", dash: "dash", width: 1 }, showlegend: false, hoverinfo: "skip" }];
  traces.push({ x: spots, y: entry, mode: "lines", name: "At entry (ADR & FX unmoved)",
    line: { color: "#8b90a0", width: 2, dash: "dot" },
    hovertemplate: "Spot %{x:,.0f}<br>entry: %{y:+,.0f} NT$<extra></extra>" });
  if (haveNow) traces.push({ x: spots, y: now, mode: "lines", name: "Now (current ADR & FX)",
    line: { color: "#4ade80", width: 2.5 },
    hovertemplate: "Spot %{x:,.0f}<br>now: %{y:+,.0f} NT$<extra></extra>" });
  const markPnl = Math.round(_payoffAtSpot(r, S0, haveNow ? _stripFx(curP, fxRatioNow) : p0, p0, haveNow ? fxRatioNow : 1, mult));
  traces.push({ x: [S0], y: [markPnl], mode: "markers", name: "Current spot",
    marker: { color: "white", size: 7 },
    hovertemplate: "Current spot %{x:,.0f}<br>P&L: %{y:+,.0f} NT$<extra></extra>" });
  // Saved positions are always long-warrant / short-option (the executable
  // direction), so the warrant strike is the leg held and the option strike
  // the leg owed. Mark both, plus spot, via the shared helper.
  const _pfMk = payoffStrikeMarks([
    { K: Kw, label: "Long W", dir: 1 },
    { K: Ko, label: "Short O", dir: -1 },
  ], S0);
  wrap.style.display = "block";
  Plotly.react(chart, traces, {
    shapes: _pfMk.shapes, annotations: _pfMk.annotations,
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#8b90a0", size: 11 },
    xaxis: { title: `Spot (NT$) — ${r.warrant_name || ""}`, gridcolor: "#252836", zerolinecolor: "#252836", tickformat: "," },
    yaxis: { title: "P&L at expiry (NT$)", gridcolor: "#252836", zerolinecolor: "#252836", tickformat: "+," },
    legend: { orientation: "h", y: 1.14, bgcolor: "rgba(0,0,0,0)" },
    margin: { l: 72, r: 16, t: 34, b: 46 }, hovermode: "x unified",
  }, { responsive: true, displayModeBar: false });
}

// The short (near-dated) leg's expiry, in ms. The trade realizes once now
// is past this — the near leg has settled and the survivor can be closed.

function _shortExpiryMs(t) {
  const r = t.row || {};
  const shortDte = Math.min(r.warrant_dte ?? 0, r.opt_dte ?? 0);
  return new Date(t.ts).getTime() + shortDte * 86400000;
}

// On load: for every open trade whose short leg has now expired, fetch the
// close basis + a live quote for the surviving leg and book realized P&L.
// Runs once per trade — t.closed is persisted so it never recomputes.

async function autoCloseTrades() {
  let changed = false;
  for (const t of portfolioData) {
    if (t.closed) continue;
    if (Date.now() < _shortExpiryMs(t)) continue;   // still active
    const r = t.row || {};
    const survivor = (r.warrant_dte ?? 0) >= (r.opt_dte ?? 0) ? "warrant" : "option";
    const shortDte = Math.min(r.warrant_dte ?? 0, r.opt_dte ?? 0);
    const optExpiryIso = new Date(new Date(t.ts).getTime() + (r.opt_dte ?? 0) * 86400000).toISOString();

    let q = {};
    try {
      const res = await api("/close_quote", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: t.mode, survivor,
          warrant_code: r.warrant_code, us_stock_code: t.us_stock_code,
          opt_type: r.opt_type, opt_strike: r.opt_strike, opt_expiry_iso: optExpiryIso,
        }),
      });
      q = await res.json();
    } catch (e) { q = {}; }

    const p0 = t.entry_premium ?? 0, fx0 = t.entry_fx ?? 1;
    const pClose  = (q.current_premium != null) ? q.current_premium : p0;
    const fxClose = (q.current_fx != null) ? q.current_fx : fx0;
    const fxRatio = (fx0 && fxClose) ? fxClose / fx0 : 1;

    // Surviving leg's real sell value, normalized into the same per-share
    // space _evPnlAtPremium values legs in (null => fall back to model BS).
    let survivorSell = null, sellSource = "model";
    if (survivor === "warrant" && q.warrant_bid) {
      const ratio = r.warrants_needed > 0 ? r.opt_contract_size / r.warrants_needed : 1;
      survivorSell = q.warrant_bid / ratio;
      sellSource = "market";
    } else if (survivor === "option" && q.opt_last_usd && q.adr_ratio) {
      survivorSell = q.opt_last_usd / q.adr_ratio * fx0;   // entry-fx TWD/share
      sellSource = "market";
    }

    const opts = { survivor, survivorSell };
    const basisClose = _stripFx(pClose, fxRatio);   // FX-stripped premium for moneyness; FX translates via fxRatio
    const realized = _evPnlAtPremium(r, basisClose, p0, fxRatio, opts) * (t.mult || 1);
    const method = _survivorExitMethod(r, survivor, shortDte, basisClose, p0, survivorSell);

    t.closed = {
      close_date: new Date(_shortExpiryMs(t)).toISOString(),
      realized_pnl: realized,
      exit_method: method,          // "sell" | "exercise"
      surviving_leg: survivor,
      sell_source: sellSource,      // "market" | "model"
      p_close: pClose, fx_close: fxClose,
    };
    changed = true;
  }
  if (changed) await savePortfolio();
}

// Which unwind pays more for the surviving leg: exercise (intrinsic) or
// sell (market quote, else BS time value). Mirrors _evPnlAtPremium.legVal.

function _survivorExitMethod(r, survivor, shortDte, p, p0, mktSell) {
  const isCall = r.warrant_type === "Call";
  const intr = (S, K) => isCall ? Math.max(0, S - K) : Math.max(0, K - S);
  let S, K, tau, iv;
  if (survivor === "warrant") {
    S = r.underlying_price; K = r.warrant_strike;
    tau = Math.max(0, (r.warrant_dte - shortDte)) / 365; iv = r.warrant_iv;
  } else {
    S = r.underlying_price * (1 + (p - p0)); K = r.opt_strike;
    tau = Math.max(0, (r.opt_dte - shortDte)) / 365; iv = r.opt_iv;
  }
  const ix = intr(S, K);
  const bs = iv ? (isCall ? bsCall(S, K, tau, R_FREE, iv) : bsPut(S, K, tau, R_FREE, iv)) : ix;
  const sell = (mktSell != null) ? mktSell : bs;
  return sell > ix ? "sell" : "exercise";
}

// Cumulative realized-P&L equity curve over close dates.

function renderPnlChart() {
  const empty = document.getElementById("portfolio-pnlEmpty");
  const chart = document.getElementById("portfolio-pnlChart");
  const closed = portfolioData
    .filter(t => t.closed)
    .sort((a, b) => new Date(a.closed.close_date) - new Date(b.closed.close_date));
  const totalEl = document.getElementById("portfolio-realized");
  if (!closed.length) {
    empty.style.display = "block"; chart.style.display = "none";
    Plotly.purge(chart);
    totalEl.textContent = "";
    return;
  }
  empty.style.display = "none"; chart.style.display = "block";
  let cum = 0;
  const x = [], y = [], text = [];
  closed.forEach(t => {
    cum += t.closed.realized_pnl;
    x.push(new Date(t.closed.close_date));
    y.push(cum);
    text.push(`${t.title || t.mode}<br>trade P&L: ${t.closed.realized_pnl>=0?"+":""}${Math.round(t.closed.realized_pnl).toLocaleString()} NT$`);
  });
  totalEl.textContent = `Realized: ${cum>=0?"+":""}${Math.round(cum).toLocaleString()} NT$`;
  totalEl.style.color = cum >= 0 ? "var(--put)" : "var(--call)";
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue("--accent").trim() || "#5b9bd5";
  const muted = css.getPropertyValue("--muted").trim() || "#888";
  Plotly.react(chart, [{
    x, y, text, mode: "lines+markers", type: "scatter",
    line: { color: accent, width: 2, shape: "hv" },
    marker: { size: 7, color: accent },
    hovertemplate: "%{x|%Y-%m-%d}<br>cumulative: %{y:,.0f} NT$<br>%{text}<extra></extra>",
  }], {
    margin: { l: 64, r: 20, t: 10, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: muted, size: 11 },
    xaxis: { gridcolor: "rgba(128,128,128,0.15)", zeroline: false },
    yaxis: { title: "Cumulative realized P&L (NT$)", gridcolor: "rgba(128,128,128,0.15)", zerolinecolor: "rgba(128,128,128,0.4)" },
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0, line: { color: muted, width: 1, dash: "dot" } }],
  }, { displayModeBar: false, responsive: true });
}

async function savePortfolio() {
  await api("/save_portfolio", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(portfolioData),
  });
}

// Short "what I bought/sold" summary from the legs array.

function legsSummary(legs) {
  return legs.map(l => `${l.action} ${l.instrument.split("—").pop().split("(")[0].trim()} @${Number(l.price).toLocaleString(undefined,{maximumFractionDigits:2})}`).join("  ·  ");
}

function enterTrade() {
  if (!_pcpRow || !_pcpLegs) return;
  // How many times to place this trade — scales every leg qty, cash flow
  // and all downstream P&L. Stored as `mult` so P&L math can re-scale.
  const mult = Math.max(1, Math.round(parseFloat(document.getElementById("pcp-trade-mult").value) || 1));
  const t = {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    ts: new Date().toISOString(),
    mode: _pcpMode,
    title: document.getElementById("pcp-modal-title").textContent,
    mult: mult,
    legs: _pcpLegs.map(l => ({ action: l.action, instrument: l.instrument, qty: l.qty * mult, price: l.price, cf: l.cf * mult })),
    net_cf: _pcpNetCf * mult,
    entry_premium: _pcpEntry ? _pcpEntry.premium : null,
    entry_fx: _pcpEntry ? _pcpEntry.fx : null,
    us_stock_code: _pcpRow.us_stock_code || null,
    row: _pcpRow,
  };
  portfolioData.push(t);
  savePortfolio();
  const btn = document.getElementById("pcp-enter-trade");
  const old = btn.textContent;
  btn.textContent = "✓ Added"; btn.style.background = "var(--accent)";
  setTimeout(() => { btn.textContent = old; btn.style.background = "var(--put)"; }, 1400);
  renderPortfolio();
}

function renderPortfolio() {
  const c = document.getElementById("portfolio-tableContainer");
  const status = document.getElementById("portfolio-status");
  if (!portfolioData.length) {
    c.innerHTML = "<p style='padding:16px;color:var(--muted)'>No trades yet. Enter one from an Arb Finder trade popup.</p>";
    status.textContent = "";
    return;
  }
  const nClosed = portfolioData.filter(t => t.closed).length;
  status.textContent = `${portfolioData.length} trade${portfolioData.length>1?"s":""} · ${nClosed} closed`;
  const modeLabel = { direct: "Direct", us: "US Opt", twus: "TW/US Opt", pcp: "PCP", uspcp: "US PCP" };
  let html = "<table><thead><tr>"
    + ["Entered","Type","×","Trade","Net premium (NT$)","P&L at expiry now (NT$)","Status","Realized P&L (NT$)",""].map(h=>`<th>${h}</th>`).join("")
    + "</tr></thead><tbody>";
  portfolioData.forEach((t, i) => {
    const d = new Date(t.ts);
    const dstr = d.toLocaleString(undefined, { year:"2-digit", month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });
    const netCol = t.net_cf >= 0 ? "put" : "call";
    // P&L at expiry at the current basis — same figure as the popup.
    const now = _pnlAtExpiryNow(t);
    let nowCell;
    if (t.closed) {
      nowCell = `<span class="mut" style="color:var(--muted)">realized</span>`;
    } else if (now == null) {
      nowCell = `<span style="color:var(--muted)">…</span>`;
    } else {
      nowCell = `<span class="${now>=0?"put":"call"}" style="font-weight:600">${now>=0?"+":""}${Math.round(now).toLocaleString()}</span>`;
    }
    let statusCell, pnlCell;
    if (t.closed) {
      const cd = new Date(t.closed.close_date).toLocaleDateString(undefined, { year:"2-digit", month:"short", day:"numeric" });
      const method = t.closed.exit_method === "sell" ? "sold" : "exercised";
      const src = t.closed.sell_source === "market" ? "" : " <span style='color:var(--muted);font-size:10px'>(model)</span>";
      statusCell = `<span style="color:var(--muted)">closed ${cd}<br><span style="font-size:10px">survivor ${method}${src}</span></span>`;
      const v = t.closed.realized_pnl;
      pnlCell = `<span class="${v>=0?"put":"call"}" style="font-weight:600">${v>=0?"+":""}${Math.round(v).toLocaleString()}</span>`;
    } else {
      const daysLeft = Math.ceil((_shortExpiryMs(t) - Date.now()) / 86400000);
      statusCell = `<span style="color:var(--accent)">active</span><br><span style="font-size:10px;color:var(--muted)">short leg ${daysLeft}d</span>`;
      pnlCell = `<span style="color:var(--muted)">—</span>`;
    }
    html += `<tr style="cursor:pointer" onclick="openPortfolioDetail('${t.id}')" title="Click for detail">
      <td>${dstr}</td>
      <td>${modeLabel[t.mode] || t.mode}</td>
      <td style="text-align:center;color:var(--muted)">${(t.mult||1)>1?"×"+(t.mult||1):""}</td>
      <td style="max-width:320px;white-space:normal;word-break:break-word;line-height:1.4" title="${legsSummary(t.legs).replace(/"/g,'&quot;')}">${legsSummary(t.legs)}</td>
      <td class="${netCol}">${t.net_cf>=0?"+":""}${Math.round(t.net_cf).toLocaleString()}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${nowCell}</td>
      <td>${statusCell}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${pnlCell}</td>
      <td style="text-align:right"><span style="color:var(--muted)">▸</span></td>
    </tr>`;
  });
  html += "</tbody></table>";
  c.innerHTML = html;
}

function closePortfolioModal() { document.getElementById("portfolioModal").style.display = "none"; }

async function openPortfolioDetail(id) {
  const t = portfolioData.find(x => x.id === id);
  if (!t) return;
  document.getElementById("pf-title").textContent = t.title;
  const held = Math.max(0, Math.round((Date.now() - new Date(t.ts)) / 86400000));
  document.getElementById("pf-sub").textContent = `Entered ${new Date(t.ts).toLocaleString()} · held ${held} day${held===1?"":"s"}`;
  document.getElementById("pf-delete").onclick = () => {
    portfolioData = portfolioData.filter(x => x.id !== id);
    savePortfolio(); renderPortfolio(); closePortfolioModal();
  };

  const th = "padding:7px 10px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border)";
  const td = "padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text)";
  const tdr = td + ";text-align:right;font-variant-numeric:tabular-nums";

  // Legs table
  let body = `<p style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--muted);margin:0 0 8px">Trade legs (at entry)</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>
    <th style="${th};text-align:left">Action</th><th style="${th};text-align:left">Instrument</th>
    <th style="${th};text-align:right">Qty</th><th style="${th};text-align:right">Price</th>
    <th style="${th};text-align:right">Cash flow</th></tr></thead><tbody>`;
  t.legs.forEach(l => {
    const ac = (l.action==="BUY"||l.action==="LEND"||l.action==="BORROW") ? "var(--put)" : "var(--call)";
    body += `<tr><td style="${td};color:${ac};font-weight:600">${l.action}</td>
      <td style="${td}">${l.instrument}</td>
      <td style="${tdr}">${Number(l.qty).toLocaleString()}</td>
      <td style="${tdr}">${Number(l.price).toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td style="${tdr};color:${l.cf>=0?'var(--put)':'var(--call)'}">${l.cf>=0?"+":""}${Math.round(l.cf).toLocaleString()}</td></tr>`;
  });
  body += `</tbody><tfoot><tr><td colspan="4" style="${td};font-weight:700;color:var(--muted);text-transform:uppercase;font-size:11px">Net premium (NT$)</td>
    <td style="${tdr};font-weight:700;color:${t.net_cf>=0?'var(--put)':'var(--call)'}">${t.net_cf>=0?"+":""}${Math.round(t.net_cf).toLocaleString()}</td></tr></tfoot></table>`;

  document.getElementById("pf-body").innerHTML = body + `<div id="pf-basis" style="margin-top:22px;color:var(--muted);font-size:12px">Loading current basis…</div>`;
  document.getElementById("portfolioModal").style.display = "block";
  // Entry-basis payoff curve up front; branches below overlay the "now" line.
  renderPfPayoff(t, null, null);

  // Closed trade: show the booked realized P&L instead of a live basis.
  if (t.closed) {
    const cl = t.closed;
    renderPfPayoff(t, cl.p_close, cl.fx_close);
    const v = cl.realized_pnl;
    const money = x => `<span style="color:${x>=0?'var(--put)':'var(--call)'}">${x>=0?"+":""}${Math.round(x).toLocaleString()}</span>`;
    const method = cl.exit_method === "sell" ? "sold to market" : "exercised (cash settled)";
    const src = cl.sell_source === "market" ? "live market quote" : "Black-Scholes model value (no live quote available)";
    document.getElementById("pf-sub").textContent =
      `Entered ${new Date(t.ts).toLocaleDateString()} · closed ${new Date(cl.close_date).toLocaleDateString()}`;
    document.getElementById("pf-basis").innerHTML = `
      <p style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--muted);margin:0 0 8px">Realized P&L</p>
      <table style="width:100%;border-collapse:collapse;font-size:13px"><tbody>
        <tr><td style="${td}">Short (near) leg</td><td style="${tdr};color:var(--muted)">expired, settled at intrinsic</td></tr>
        <tr><td style="${td}">Surviving ${cl.surviving_leg} leg</td><td style="${tdr}">${method}</td></tr>
        <tr><td style="${td}">Sell value source</td><td style="${tdr};color:var(--muted)">${src}</td></tr>
        <tr><td style="${td};font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.4px;color:var(--muted)">Realized P&L (NT$)</td><td style="${tdr};font-weight:700;font-size:15px">${money(v)}</td></tr>
      </tbody></table>
      <div style="background:var(--surface);border-radius:6px;padding:12px 14px;margin-top:12px;font-size:12px;color:var(--muted);line-height:1.6">
        Booked when the short leg expired. The surviving leg was valued at whichever paid more — exercise (intrinsic) vs sell to market — using the ${cl.sell_source==="market"?"live":"model"} value at close. Premium/FX at close: ${cl.p_close!=null?(cl.p_close*100).toFixed(2)+"%":"—"} / ${cl.fx_close!=null?cl.fx_close.toFixed(3):"—"}.
      </div>`;
    return;
  }

  // Direct Match / PCP: no ADR/FX basis.
  if (t.mode === "direct" || t.mode === "pcp" || !t.us_stock_code || t.entry_premium == null) {
    const legNote = t.mode === "pcp"
      ? "Put-call-parity trade — P&L is set by the warrant, option, underlying and risk-free bond legs held to expiry (dividends ignored; a pair spanning an ex-dividend date is mispriced)."
      : "Same-underlying trade — no ADR premium / FX basis to track. P&L is set by the two Taiwan legs held to expiry.";
    document.getElementById("pf-basis").innerHTML =
      `<div style="background:var(--surface);border-radius:6px;padding:12px 14px;line-height:1.6">${legNote}</div>`;
    return;
  }

  // Fetch current premium + FX and compute P&L at expiry at the current basis.
  const row = t.row;
  const horizon = Math.max(1, Math.min(row.warrant_dte, row.opt_dte));
  let s;
  try {
    const res = await api("/adr_premium_scenario", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stock_code: t.us_stock_code, horizon_days: horizon }),
    });
    s = await res.json();
  } catch (e) { document.getElementById("pf-basis").textContent = "current basis unavailable"; return; }
  if (s.error) { document.getElementById("pf-basis").textContent = s.error; return; }

  const curP = s.current_premium, curFx = s.fx.current_fx;
  const p0 = t.entry_premium, fx0 = t.entry_fx;
  const fxRatio = (fx0 && curFx) ? curFx / fx0 : 1;
  renderPfPayoff(t, curP, curFx);   // overlay current-basis payoff line
  const dPrem = (curP - p0) * 100;              // premium move, pct points
  const dFx = fx0 ? (curFx / fx0 - 1) * 100 : 0; // FX move, %

  // P&L at expiry at current basis, plus the premium-only / FX split.
  // Intrinsic-only floor at the current spot — both legs at intrinsic, the
  // surviving (long) leg's time value ignored (conservative). The column
  // and this popup use the identical _payoffAtSpot valuation.
  const mult = t.mult || 1;
  const S0 = row.underlying_price;
  const basisNow = _stripFx(curP, fxRatio);   // premium with FX stripped out (moneyness)
  const pnlNow = _payoffAtSpot(row, S0, basisNow, p0, fxRatio, mult);
  const pnlPremOnly = _payoffAtSpot(row, S0, basisNow, p0, 1, mult);   // basis moved, FX flat
  const pnlEntry = _payoffAtSpot(row, S0, p0, p0, 1, mult);
  const fxEffect = pnlNow - pnlPremOnly;      // pure FX translation
  const premEffect = pnlPremOnly - pnlEntry;  // pure basis (premium ex-FX)

  const fmtSigned = (v, suf) => (v>=0?"+":"") + v.toFixed(2) + (suf||"");
  const remDte = Math.max(0, horizon - Math.round((Date.now()-new Date(t.ts))/86400000));
  const money = v => `<span style="color:${v>=0?'var(--put)':'var(--call)'}">${v>=0?"+":""}${Math.round(v).toLocaleString()}</span>`;

  document.getElementById("pf-basis").innerHTML = `
    <p style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--muted);margin:0 0 8px">Basis move since entry</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>
      <th style="${th};text-align:left"></th><th style="${th};text-align:right">At entry</th>
      <th style="${th};text-align:right">Now</th><th style="${th};text-align:right">Move</th></tr></thead><tbody>
      <tr><td style="${td}">ADR premium</td><td style="${tdr}">${fmtSigned(p0*100,"%")}</td><td style="${tdr}">${fmtSigned(curP*100,"%")}</td><td style="${tdr};color:${Math.abs(dPrem)<0.01?'var(--muted)':(dPrem>0?'var(--put)':'var(--call)')}">${fmtSigned(dPrem,"pp")}</td></tr>
      <tr><td style="${td}">FX (TWD/USD)</td><td style="${tdr}">${fx0.toFixed(3)}</td><td style="${tdr}">${curFx.toFixed(3)}</td><td style="${tdr};color:${Math.abs(dFx)<0.01?'var(--muted)':(dFx>0?'var(--put)':'var(--call)')}">${fmtSigned(dFx,"%")}</td></tr>
    </tbody></table>

    <p style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--muted);margin:20px 0 8px">P&L at expiry, at current basis</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px"><tbody>
      <tr><td style="${td}">Entry baseline (premium & FX at entry)</td><td style="${tdr}">${money(pnlEntry)}</td></tr>
      <tr><td style="${td}">+ premium move (ex-FX basis)</td><td style="${tdr}">${money(premEffect)}</td></tr>
      <tr><td style="${td}">+ FX move effect</td><td style="${tdr}">${money(fxEffect)}</td></tr>
      <tr><td style="${td};font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.4px;color:var(--muted)">P&L at expiry now</td><td style="${tdr};font-weight:700;font-size:14px">${money(pnlNow)}</td></tr>
    </tbody></table>
    <div style="background:var(--surface);border-radius:6px;padding:12px 14px;margin-top:12px;font-size:12px;color:var(--muted);line-height:1.6">
      Assumes ADR premium & FX hold at their current levels to the first expiry (${remDte} calendar days out), underlying delta-hedged. Premium/FX are delayed quotes. This is a model estimate, not a live mark.
    </div>`;
}

document.getElementById("portfolioModal").addEventListener("click", e => {
  if (e.target === document.getElementById("portfolioModal")) closePortfolioModal();
});

async function _boot() {
  if (!LOCAL_MODE && SUPABASE_URL && !_sb) {
    // Auth is configured but supabase-js never loaded (CDN blocked).
    document.body.innerHTML =
      '<div style="max-width:420px;margin:15vh auto;text-align:center;font-family:inherit;color:#e2e8f0">' +
      '<h2 style="font-size:18px;margin-bottom:10px">Could not load the sign-in library</h2>' +
      '<p style="color:#8b90a0;font-size:13px">The script from cdn.jsdelivr.net was blocked. Disable ad-blockers for this site and reload.</p></div>';
    return;
  }
  if (_sb) {
    const { data } = await _sb.auth.getSession();
    if (!data.session) { location.replace("/login"); return; }
    document.getElementById("logoutBtn").style.display = "block";
    _sb.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_OUT" || !session) location.replace("/login");
    });
  }
  await initProductSelects();
  // Portfolio is loaded lazily on first Portfolio-tab click (loadPortfolioOnce),
  // not here — the landing tab doesn't use it. Saves /get_portfolio +
  // /adr_premium_scenario calls on every reload.
  restoreView();   // put the user back on the tab/market they had before reload
}

_boot();
