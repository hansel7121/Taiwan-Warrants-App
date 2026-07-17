// Quant: Black-Scholes greeks + per-share P&L / expected-value math (no DOM).

let _premEvNt = 0;   // premium EV (FX-stripped) — set by loadAdrScenario, read by renderFxSection for the total
//
// Both legs are the SAME underlying, so the pair is delta-matched and the
// dominant residual risk is the ADR *basis* (premium). We hold the
// underlying at current spot S0 (direction is hedged) and let only the
// ADR premium vary. The warrant settles on the local price S0; the option
// settles on the ADR-implied local price S0·(1 + (p − p0)), where p is the
// exit premium and p0 the premium at entry (today's).
//
//   PnL(p) = dir · [ (Wintrinsic(S0,Kw) − Wentry)
//                    + (Oentry − Ointrinsic(S0·(1+(p−p0)),Ko)) ] · shares
//
//   EV = Σ_regime  P(regime) · PnL( conditional-mean premium of regime )
//
// Regimes (spike / around-0 / drop) and their probabilities + conditional
// mean premia come from the historical premium series (backend).
// Strip the FX-driven part out of a premium reading, leaving the FX-flat
// "basis" premium. Identity: 1+prem = (1+p0)·fxRatio·exp(Δb), so the
// FX-stripped effective premium is (1+prem)/fxRatio − 1. The P&L engines
// price option moneyness off this basis premium and translate FX
// separately (fxRatio), so FX is never counted twice.

const _stripFx = (prem, fxRatio) => (1 + prem) / (fxRatio || 1) - 1;

// `p` is the FX-STRIPPED (basis) premium: option moneyness = S0·(1+(p−p0))
// reflects only the ADR-vs-local divergence, and `fxRatio` translates the
// USD option leg to TWD. Callers holding an actual (FX-laden) premium must
// pass _stripFx(premium, fxRatio) here.

function _evPnlAtPremium(row, p, p0, fxRatio, opts) {
  const fx = (fxRatio == null) ? 1 : fxRatio;   // F_exit / F_today on the US leg
  const isCall = row.warrant_type === "Call";
  const S0 = row.underlying_price, Kw = row.warrant_strike, Ko = row.opt_strike;
  const wEntry = row.warrant_per_share, oEntry = row.synthetic_price;
  const shares = row.opt_contract_size;   // exact-hedge share count
  const dir = row.pcp_diff >= 0 ? 1 : -1;
  const optSpot = S0 * (1 + (p - p0));    // ADR basis moves the option leg only
  const intr = (S, K) => isCall ? Math.max(0, S - K) : Math.max(0, K - S);

  // The trade unwinds at the FIRST expiry. The leg that has already expired
  // settles at intrinsic; the leg still alive is marked at its Black-Scholes
  // value with the remaining time (needs IV) — otherwise a not-yet-expired
  // short leg looks worthless and the P&L is flat across premium regimes.
  const H = Math.max(1, Math.min(row.warrant_dte ?? 0, row.opt_dte ?? 0));
  const tauW = Math.max(0, (row.warrant_dte - H)) / 365;
  const tauO = Math.max(0, (row.opt_dte - H)) / 365;
  // At the first expiry the near leg is done (settles intrinsic). The far
  // (surviving) leg is unwound by rule: EXERCISE if ITM (take intrinsic),
  // otherwise SELL it back to the market (its time value = BS value, since
  // OTM intrinsic is 0). BS needs IV — if absent, fall back to intrinsic.
  // Survivor unwind: ALWAYS compare EXERCISE (take intrinsic) against
  // SELL to market and keep whichever pays more — selling captures time
  // value, so it usually beats exercising even when ITM. Sell value =
  // real market quote (mktSell) when we have one, else the BS value.
  const legVal = (S, K, tau, iv, mktSell) => {
    const ix = intr(S, K);
    if (tau <= 0) return ix;              // near leg: expired -> settles intrinsic
    const bs = iv ? (isCall ? bsCall(S, K, tau, R_FREE, iv) : bsPut(S, K, tau, R_FREE, iv)) : ix;
    const sell = (mktSell != null) ? mktSell : bs;
    return Math.max(ix, sell);
  };
  const wSell = (opts && opts.survivor === "warrant") ? opts.survivorSell : null;
  const oSell = (opts && opts.survivor === "option")  ? opts.survivorSell : null;
  let wVal = legVal(S0, Kw, tauW, row.warrant_iv, wSell);
  let oVal = legVal(optSpot, Ko, tauO, row.opt_iv, oSell);

  // Loose mode: price BOTH entry and exit at the favorable side of the
  // spread (buy@bid, sell@ask). The entry already flips loose in the
  // backend matcher; here we also unwind loose — sell the long (warrant)
  // leg at its ask and buy back the short (option) leg at its bid, using
  // today's half-spread as the exit-spread proxy. Only the tradeable
  // positive direction (dir>0) has a loose toggle.
  if (row.loose && dir > 0 && !opts) {
    const ratio = row.warrants_needed > 0 ? row.opt_contract_size / row.warrants_needed : 1;
    const hw = (row.warrant_ask != null && row.warrant_bid != null)
      ? (row.warrant_ask - row.warrant_bid) / 2 / ratio : 0;   // per-share
    const ho = (row.opt_ask != null && row.opt_bid != null)
      ? (row.opt_ask - row.opt_bid) / 2 : 0;                    // already per-share
    wVal += hw;   // exit: sell the warrant at its ask
    oVal -= ho;   // exit: buy back the option at its bid
  }

  // Short-option TWD P&L = premium received at entry FX − buyback at exit
  // FX = oEntry − fxRatio·oVal (oEntry/oVal are entry-FX TWD/share; only
  // the exit leg re-translates). NOT fx·(oEntry−oVal): the entry premium
  // is already fixed in TWD, so scaling it by FX would double-count. The
  // TW (warrant) leg is already TWD and unaffected.
  const perShare = dir * ((wVal - wEntry) + (oEntry - fx * oVal));
  return perShare * shares;
}

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

// Warrant tau is offset from the option: warrant may have more days to expiry.

function warrantTau(row, tauOpt) {
  const dteDiff = ((row.warrant_dte || row.opt_dte) - row.opt_dte) / 365;
  return Math.max(0, tauOpt + dteDiff);
}
// Per-underlying-share P&L of the long-warrant leg (entry = warrant price/share).

function warrantPnLPerShare(S, row, tauOpt) {
  const iv = row.warrant_iv || 0.4, K = row.warrant_strike, entry = row.warrant_per_share;
  const tauW = warrantTau(row, tauOpt);
  const v = row.warrant_type === "Call" ? bsCall(S,K,tauW,R_FREE,iv) : bsPut(S,K,tauW,R_FREE,iv);
  return v - entry;
}
// Per-underlying-share P&L of the short (synthetic) option leg (entry = synthetic price/share).

function optionPnLPerShare(S, row, tauOpt) {
  const iv = row.opt_iv || 0.4, K = row.opt_strike, entry = row.synthetic_price;
  const v = row.warrant_type === "Call" ? bsCall(S,K,tauOpt,R_FREE,iv) : bsPut(S,K,tauOpt,R_FREE,iv);
  return entry - v;
}
// Combined per-share P&L of the matched pair (long warrant + short option).
// Sum of the two legs === pcp_diff + warrantVal − optVal.

function pnlPerShare(S, row, tauOpt) {
  return warrantPnLPerShare(S, row, tauOpt) + optionPnLPerShare(S, row, tauOpt);
}
