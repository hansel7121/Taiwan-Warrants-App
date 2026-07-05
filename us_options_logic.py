"""US ADR option chain (UMC) normalized into TWD-per-Taiwan-share space.

The Taiwan ordinary (2303) and the US ADR (UMC) are the same underlying. One US
option contract controls 100 ADR, and 1 ADR = 5 ordinary shares, so a single US
contract controls 100 * 5 = 500 Taiwan shares.

To let a US option be compared directly against a Taiwan warrant we express its
prices and strike in **TWD per Taiwan share**:

    twd_per_tw_share = usd_per_ADR / adr_ratio * FX      (FX = TWD per USD)

Implied vol is scale-free (invariant to currency and to the ADR ratio), so it is
computed once in native USD/ADR space and carried over unchanged.

FX is assumed constant over the holding period (per product decision), so a
single spot FX snapshot is used for every conversion.
"""

import time
import numpy as np
import pandas as pd
import yfinance as yf

from warrant_logic import implied_vol, bs_delta, calc_real_leverage

# One US option contract covers 100 ADRs. The number of ordinary (Taiwan)
# shares per ADR ("adr_ratio") is per-listing, so contract size in Taiwan
# shares = 100 * adr_ratio.
US_CONTRACT_ADRS = 100

R_US = 0.04  # US benchmark rate for BS/IV on the ADR leg

# Map a Taiwan stock code to its US ADR ticker, FX pair, and ADR ratio
# (ordinary shares per ADR). Only listings whose ADR tracks the local share
# tightly enough to treat as the same asset are included — see the parity
# screen in analysis notebooks (persistent/large premium => excluded, e.g. TSM).
US_ADR_MAP = {
    "2303": {"adr_ticker": "UMC", "fx_ticker": "TWD=X", "adr_ratio": 5},   # 500 TW shares/contract
    "2412": {"adr_ticker": "CHT", "fx_ticker": "TWD=X", "adr_ratio": 10},  # 1000 TW shares/contract
}


def contract_tw_shares(stock_code):
    """Taiwan shares controlled by one US option contract for this listing."""
    return US_CONTRACT_ADRS * US_ADR_MAP[stock_code]["adr_ratio"]

_cache: dict = {}
_CACHE_TTL = 300  # 5 minutes

_prem_cache: dict = {}
_PREM_TTL = 1800  # 30 minutes

# ADR premium is the % gap between the ADR (converted to TWD/share) and the
# local Taiwan share. Regime thresholds: |p| <= 5% is "around 0" (the safe
# entry zone); above/below is a rich/cheap basis dislocation.
PREM_THRESHOLD = 0.05


def _premium_series(stock_code, period="3y"):
    """Return (df, prem) where prem_t = (adr_usd/adr_ratio*fx)/tw_close − 1.

    The premium already embeds FX (it's baked into the numerator), so its
    distribution captures the *combined* ADR-basis + FX risk of the trade.
    """
    cfg = US_ADR_MAP[stock_code]

    def daily(t):
        s = yf.Ticker(t).history(period=period)["Close"]
        s.index = pd.to_datetime(s.index.date)
        return s[~s.index.duplicated()]

    a = daily(cfg["adr_ticker"])
    t = daily(f"{stock_code}.TW")
    fx = daily(cfg["fx_ticker"])
    df = pd.DataFrame({"a": a, "t": t})
    df["fx"] = fx.reindex(df.index).ffill()
    df = df.dropna()
    if df.empty:
        raise RuntimeError("no overlapping ADR/TW history")

    adr_tw = df["a"] / cfg["adr_ratio"] * df["fx"]
    prem = (adr_tw / df["t"] - 1.0)  # fraction
    return df, prem


def adr_premium_scenario(stock_code, horizon_days, period="3y"):
    """Horizon-conditional premium outlook for entering the trade *today*.

    Given the current premium regime (spike / around-0 / drop) and the trade's
    holding horizon (calendar days to the first expiry), estimate empirically:
    starting from days historically in the *same* regime as now, where did the
    premium land ``horizon`` days later? Returns the transition probabilities to
    each end-regime plus each bucket's conditional-mean end premium — the inputs
    to a realistic "if premium is high now, what happens by expiry" EV.
    """
    df, prem = _premium_series(stock_code, period)
    p = prem.values.astype(float)
    F = df["fx"].values.astype(float)          # TWD per USD, aligned with premium
    n = len(p)
    p0 = float(p[-1])
    F0 = float(F[-1])

    # Trading-day horizon ≈ calendar days × (252/365).
    k = max(1, int(round(float(horizon_days) * 252.0 / 365.0)))
    k = min(k, max(1, n - 2))

    def regime_of(x):
        if x > PREM_THRESHOLD:
            return "hi"
        if x < -PREM_THRESHOLD:
            return "lo"
        return "mid"

    cur = regime_of(p0)
    cur_label = {"hi": f"Spike > +{int(PREM_THRESHOLD*100)}%",
                 "mid": f"Around 0 (|p| ≤ {int(PREM_THRESHOLD*100)}%)",
                 "lo": f"Drop < -{int(PREM_THRESHOLD*100)}%"}[cur]

    starts_p = p[: n - k]
    ends_p = p[k:]
    fx_ratio = F[k:] / F[: n - k]              # F_exit / F_entry for each window

    # ── Premium: condition on windows that STARTED near today's premium ──
    conditional = True
    band = None
    for b in (0.02, 0.04, 0.06):
        m = np.abs(starts_p - p0) <= b
        if int(m.sum()) >= 30:
            pmask = m
            band = b
            break
    else:
        pmask = np.ones(len(starts_p), dtype=bool)
        conditional = False

    ends_cond = ends_p[pmask]
    fx_cond = fx_ratio[pmask]                   # paired FX move for the SAME window

    def bucket(mask, label):
        prob = float(mask.mean()) if len(mask) else 0.0
        cond = float(ends_cond[mask].mean()) if mask.any() else 0.0
        # Paired (forward premium, forward FX ratio) for every conditioned window
        # that landed in this band, so the frontend can average P&L jointly over
        # premium AND FX (E[PnL|band]) — nonlinear, so not PnL at the means.
        return {
            "label": label, "prob": prob, "cond_premium": cond,
            "premiums": [round(float(x), 6) for x in ends_cond[mask].tolist()],
            "fx_ratios": [round(float(x), 6) for x in fx_cond[mask].tolist()],
        }

    hi = ends_cond > PREM_THRESHOLD
    lo = ends_cond < -PREM_THRESHOLD
    mid = ~hi & ~lo

    # ── FX orthogonalized against premium (how much FX is NOT explained by it) ──
    rp = np.diff(np.log1p(p))
    rf = np.diff(np.log(F))
    if rp.std() > 0 and rf.std() > 0:
        corr = float(np.corrcoef(rp, rf)[0, 1])
        beta = float(np.cov(rf, rp)[0, 1] / np.var(rp))
        resid = rf - (rf.mean() + beta * (rp - rp.mean()))
    else:
        corr, beta, resid = 0.0, 0.0, rf - rf.mean()
    r2 = corr ** 2                             # shared variance fraction

    # ── FX factor: condition on windows that STARTED near today's FX level ──
    starts_f = F[: n - k]
    fx_band = None
    for fb in (0.01, 0.02, 0.03):
        mf = np.abs(starts_f / F0 - 1.0) <= fb
        if int(mf.sum()) >= 30:
            fmask = mf
            fx_band = fb
            break
    else:
        fmask = np.ones(len(starts_f), dtype=bool)
    fx_cond2 = fx_ratio[fmask]
    stay = np.abs(fx_cond2 - 1.0) <= 0.005     # within ±0.5% = "stays"
    up = fx_cond2 > 1.005                       # TWD weakens (more TWD per USD)
    down = fx_cond2 < 0.995                      # TWD strengthens
    fx_chg_30d = float((F0 / F[-22] - 1) * 100) if n > 22 else 0.0
    fx_panel = {
        "current_fx": round(F0, 4),
        "chg_30d_pct": round(fx_chg_30d, 3),
        "cond_band_pct": round(fx_band * 100, 1) if fx_band is not None else None,
        "n": int(len(fx_cond2)),
        "stay_prob": float(stay.mean()),
        "up_prob": float(up.mean()),
        "down_prob": float(down.mean()),
        "up_mean_pct": float((fx_cond2[up].mean() - 1) * 100) if up.any() else 0.0,
        "down_mean_pct": float((fx_cond2[down].mean() - 1) * 100) if down.any() else 0.0,
        # forward FX % move for every FX-conditioned window (for the modal)
        "ratios_pct": [round(float((x - 1) * 100), 4) for x in fx_cond2.tolist()],
        "r2_with_premium": round(r2, 4),        # coupling of FX & premium moves
        "corr_with_premium": round(corr, 4),
        "unexplained_frac": round(1 - r2, 4),   # FX variance NOT explained by premium
    }

    return {
        "stock_code": stock_code,
        "horizon_days": int(horizon_days),
        "horizon_trading": int(k),
        "current_premium": p0,
        "current_regime": cur,
        "current_regime_label": cur_label,
        "conditional": conditional,   # False = fell back to unconditional
        "band_pct": round(band * 100, 1) if band is not None else None,
        "n_samples": int(len(ends_cond)),
        "regimes": [
            bucket(hi, f"Spike > +{int(PREM_THRESHOLD*100)}%"),
            bucket(mid, f"Around 0 (|p| ≤ {int(PREM_THRESHOLD*100)}%)"),
            bucket(lo, f"Drop < -{int(PREM_THRESHOLD*100)}%"),
        ],
        "fx": fx_panel,
        # Series for the FX charts (level over time + isolated residual moves).
        "fx_series": {
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "level": [round(float(x), 4) for x in F.tolist()],
            "resid_pct": [round(float(x) * 100, 4) for x in resid.tolist()],
            # Daily % changes for the premium-vs-FX correlation scatter.
            "dprem_pct": [round(float(x) * 100, 4) for x in rp.tolist()],
            "dfx_pct": [round(float(x) * 100, 4) for x in rf.tolist()],
        },
    }


def adr_premium_stats(stock_code, period="3y"):
    """Historical ADR-vs-local premium series + regime probabilities.

    premium_t = (adr_usd_t / adr_ratio * fx_t) / tw_close_t - 1

    Returns dates, premium %, the latest premium, and 3 regimes (high / mid /
    low) with their historical frequency and conditional-mean premium — the
    inputs to the Expected-Value calc in the trade modal.
    """
    cfg = US_ADR_MAP[stock_code]
    key = (stock_code, period)
    hit = _prem_cache.get(key)
    if hit and time.time() - hit[0] < _PREM_TTL:
        return hit[1]

    df, prem = _premium_series(stock_code, period)

    hi = prem > PREM_THRESHOLD
    lo = prem < -PREM_THRESHOLD
    mid = ~hi & ~lo

    def regime(mask, label):
        p = float(mask.mean())
        cond = float(prem[mask].mean()) if mask.any() else 0.0
        return {"label": label, "prob": p, "cond_premium": cond}

    stats = {
        "stock_code": stock_code,
        "adr_ticker": cfg["adr_ticker"],
        "adr_ratio": cfg["adr_ratio"],
        "period": period,
        "n": int(len(df)),
        "latest_premium": float(prem.iloc[-1]),
        "mean_premium": float(prem.mean()),
        "std_premium": float(prem.std()),
        "threshold": PREM_THRESHOLD,
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        "premium_pct": [round(float(x) * 100, 3) for x in prem],
        "regimes": [
            regime(hi, f"Spike > +{int(PREM_THRESHOLD*100)}%"),
            regime(mid, f"Around 0 (|p| ≤ {int(PREM_THRESHOLD*100)}%)"),
            regime(lo, f"Drop < -{int(PREM_THRESHOLD*100)}%"),
        ],
    }
    _prem_cache[key] = (time.time(), stats)
    return stats


def _last_price(ticker: str) -> float:
    tk = yf.Ticker(ticker)
    # fast_info is cheap and usually populated; fall back to history.
    try:
        p = float(tk.fast_info["last_price"])
        if p > 0:
            return p
    except Exception:
        pass
    hist = tk.history(period="5d")
    if hist.empty:
        raise RuntimeError(f"no price for {ticker}")
    return float(hist["Close"].dropna().iloc[-1])


def fetch_us_options(stock_code, option_type="All", min_days=1, max_days=365,
                     compute_iv=True):
    """Return a DataFrame of UMC options priced in TWD per Taiwan share.

    Columns mirror options_logic.fetch_options so the same matching code can
    consume it: type, strike, days_to_expiry, bid, ask, iv_bid, iv_ask,
    contract, is_live, plus USD reference fields (strike_usd, fx, adr_price).
    """
    if stock_code not in US_ADR_MAP:
        raise RuntimeError(f"{stock_code}: no US ADR mapping")

    cfg = US_ADR_MAP[stock_code]
    cache_key = (stock_code, option_type, min_days, max_days)
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1].copy()

    adr_ratio = cfg["adr_ratio"]             # ordinary shares per ADR
    adr = _last_price(cfg["adr_ticker"])     # USD per ADR
    fx = _last_price(cfg["fx_ticker"])       # TWD per USD
    if adr <= 0 or fx <= 0:
        raise RuntimeError("bad ADR price or FX")

    # Underlying value expressed per Taiwan share, in TWD — the same basis the
    # warrant leg uses (so strike_diff_pct etc. are apples-to-apples).
    S_twd = adr / adr_ratio * fx

    tk = yf.Ticker(cfg["adr_ticker"])
    expiries = tk.options
    if not expiries:
        raise RuntimeError(f"{cfg['adr_ticker']}: no option expiries")

    today = pd.Timestamp.now().normalize()
    rows = []
    for exp in expiries:
        exp_ts = pd.Timestamp(exp)
        dte = int((exp_ts - today).days)
        if dte < int(min_days) or dte > int(max_days):
            continue
        try:
            chain = tk.option_chain(exp)
        except Exception:
            continue

        for is_put, leg in ((False, chain.calls), (True, chain.puts)):
            opt_type = "Put" if is_put else "Call"
            if option_type != "All" and opt_type != option_type:
                continue
            for _, o in leg.iterrows():
                K_usd = float(o.get("strike", np.nan))
                bid_usd = float(o.get("bid", np.nan) or np.nan)
                ask_usd = float(o.get("ask", np.nan) or np.nan)
                last_usd = float(o.get("lastPrice", np.nan) or np.nan)
                if not np.isfinite(K_usd) or K_usd <= 0:
                    continue

                ask_live = np.isfinite(ask_usd) and ask_usd > 0
                bid_live = np.isfinite(bid_usd) and bid_usd > 0
                is_live = ask_live and bid_live

                # Fall back to last trade when a side is missing.
                a_usd = ask_usd if ask_live else last_usd
                b_usd = bid_usd if bid_live else last_usd
                if not (np.isfinite(a_usd) and a_usd > 0):
                    continue

                T = dte / 365.0
                if compute_iv:
                    # IV in native USD/ADR space (scale-free).
                    iv_ask = implied_vol(a_usd, adr, K_usd, T, R_US, 1.0, is_put)
                    iv_bid = (
                        implied_vol(b_usd, adr, K_usd, T, R_US, 1.0, is_put)
                        if np.isfinite(b_usd) and b_usd > 0
                        else np.nan
                    )
                    if np.isnan(iv_ask) and not np.isnan(iv_bid):
                        iv_ask = iv_bid
                    if np.isnan(iv_ask):
                        continue

                    delta = bs_delta(adr, K_usd, T, R_US, iv_ask, 1.0, is_put)
                else:
                    # Arb finder does not use IV/delta — skip the solve so an
                    # option is never dropped just because IV wouldn't converge.
                    iv_ask = iv_bid = delta = np.nan

                conv = fx / adr_ratio  # USD/ADR -> TWD/Taiwan-share
                strike_twd = K_usd * conv
                ask_twd = a_usd * conv
                bid_twd = (b_usd * conv) if (np.isfinite(b_usd) and b_usd > 0) else np.nan

                contract = f"{'P' if is_put else 'C'}{K_usd:g} {exp_ts.strftime('%b%y')} (US)"

                rows.append({
                    "contract": contract,
                    "type": opt_type,
                    "underlying_price": round(S_twd, 4),
                    "strike": round(strike_twd, 4),
                    "days_to_expiry": dte,
                    "bid": round(bid_twd, 4) if np.isfinite(bid_twd) else None,
                    "ask": round(ask_twd, 4),
                    "iv_ask": round(float(iv_ask), 4),
                    "iv_bid": round(float(iv_bid), 4) if np.isfinite(iv_bid) else None,
                    "delta_calc": round(float(delta), 4),
                    "volume": int(o["volume"]) if pd.notna(o.get("volume")) else 0,
                    "oi": int(o["openInterest"]) if pd.notna(o.get("openInterest")) else 0,
                    "is_live": bool(is_live),
                    # USD reference (for display / auditing)
                    "strike_usd": round(K_usd, 4),
                    "adr_price": round(adr, 4),
                    "fx": round(fx, 4),
                })

    if not rows:
        raise RuntimeError("no US options in range")

    df = pd.DataFrame(rows).sort_values(["days_to_expiry", "strike"]).reset_index(drop=True)
    _cache[cache_key] = (time.time(), df.copy())
    return df
