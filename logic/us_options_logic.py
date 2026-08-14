"""US ADR option chain fetch, normalized into TWD-per-Taiwan-share space so it can
be compared directly against a Taiwan warrant: `twd_per_tw_share = usd_per_ADR /
adr_ratio * FX`. Implied vol is scale-free, so it's computed once in native
USD/ADR space and carried over unchanged; FX is held constant per snapshot.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import yfinance as yf
from statsmodels.tsa.stattools import adfuller

from services import applog
from services import db_market
from logic.warrant_logic import (
    implied_vol, bs_delta, calc_real_leverage,
    implied_vol_vec, bs_delta_vec, _refine_iv_for_rounding,
)
from logic.ttl_cache import TTLCache

# One US option contract covers 100 ADRs. The number of ordinary (Taiwan)
# shares per ADR ("adr_ratio") is per-listing, so contract size in Taiwan
# shares = 100 * adr_ratio.
US_CONTRACT_ADRS = 100

R_US = 0.04  # US benchmark rate for BS/IV on the ADR leg

_PRODUCT_CACHE_TTL = 300  # 5 min; add/remove routes also invalidate directly
_adr_map_cache = TTLCache("us_option_products", _PRODUCT_CACHE_TTL)


def _load_adr_map():
    from services import db_products
    return {row["code"]: row for row in db_products.list_us_option_products()}


def _adr_map():
    """{code: {adr_ticker, fx_ticker, adr_ratio, name}} from us_option_products, cached."""
    return _adr_map_cache.get_or_set("all", _load_adr_map)


def invalidate_adr_map_cache():
    """Called by the add/remove product routes (Phase 5.3) so a change is visible immediately."""
    _adr_map_cache.invalidate()


def contract_tw_shares(stock_code):
    """Taiwan shares controlled by one US option contract for this listing."""
    return US_CONTRACT_ADRS * _adr_map()[stock_code]["adr_ratio"]

# Full option chain per (stock_code, compute_iv); day-range and type filters
# are applied per call so one scheduler warm-up serves every filter combo.
_CACHE_TTL = 1200  # refreshed every 15 min by the scheduler (Yahoo is ~15 min delayed anyway)
_cache = TTLCache("us_option_chains", _CACHE_TTL)


def data_as_of(stock_code):
    """Timestamp (epoch) of the cached chain for this code, or None."""
    # Snapshot mode: freshness is the snapshot batch's created_at as an epoch.
    if db_market.snapshot_enabled():
        try:
            iso = db_market.snapshot_as_of("us_options")
        except Exception:
            iso = None
        if iso:
            return datetime.fromisoformat(iso).timestamp()
        return None
    ts_list = [
        ts for key, ts, _df in _cache.items() if key[0] == stock_code
    ]
    return min(ts_list) if ts_list else None


_prem_cache: dict = {}
_PREM_TTL = 1800  # 30 minutes — 3y premium HISTORY only; the "now" point is refreshed live

# ADR premium is the % gap between the ADR (converted to TWD/share) and the
# local Taiwan share. Regime thresholds: |p| <= 5% is "around 0" (the safe
# entry zone); above/below is a rich/cheap basis dislocation.
PREM_THRESHOLD = 0.05


def _premium_series(stock_code, period="3y"):
    """Return (df, prem) where prem_t = (adr_usd/adr_ratio*fx)/tw_close − 1.

    The premium already embeds FX (it's baked into the numerator), so its
    distribution captures the *combined* ADR-basis + FX risk of the trade.
    """
    cfg = _adr_map()[stock_code]

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
    # "Now" = freshest intraday snapshot, NOT the last daily close. The 3y
    # history (cached) only supplies the move distribution; the current premium
    # and FX come live so the entry basis matches real-time execution.
    p0 = float(p[-1])
    F0 = float(F[-1])
    live_now = False
    _live = _live_premium_fx(stock_code)
    if _live is not None:
        p0, F0 = _live
        live_now = True

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

    # Premium move stripped of FX (exact identity): Δln(1+prem) = Δln(FX) + Δb,
    # where Δb is the residual "basis" move not explained by FX. The option leg's
    # moneyness is driven by Δb only; FX is priced as its own separate EV line.
    lp = np.log1p(p)
    lf = np.log(F)
    db = (lp[k:] - lp[: n - k]) - (lf[k:] - lf[: n - k])   # residual basis log-move / window
    # Apply the FX-stripped move to today's premium with FX held flat:
    #   1 + p_eff = (1 + p0)·exp(Δb)
    ends_cond = (1.0 + p0) * np.exp(db) - 1.0
    conditional = True
    band = None

    def bucket(mask, label):
        prob = float(mask.mean()) if len(mask) else 0.0
        cond = float(ends_cond[mask].mean()) if mask.any() else 0.0
        # FX-stripped exit premiums for this bucket. The frontend prices the
        # option leg's moneyness off these with FX held flat (fxRatio = 1); FX
        # is handled as its own EV line, so FX is never counted twice.
        return {
            "label": label, "prob": prob, "cond_premium": cond,
            "premiums": [round(float(x), 6) for x in ends_cond[mask].tolist()],
        }

    C = PREM_THRESHOLD                            # ±5% band on the FX-stripped basis move
    lo = db < -C                                  # basis drops >5% below today
    hi = db > C                                   # basis rises >5% above today
    mid = ~lo & ~hi                               # stays within ±5% of today
    thr = int(C * 100)
    if p0 < 0:
        lo_lbl, mid_lbl, hi_lbl = (f"Bigger drop (≥{thr}% below now)",
                                   f"Stays (±{thr}% of now)",
                                   f"Reverts up (≥{thr}% above now)")
    elif p0 > 0:
        lo_lbl, mid_lbl, hi_lbl = (f"Reverts down (≥{thr}% below now)",
                                   f"Stays (±{thr}% of now)",
                                   f"Bigger spike (≥{thr}% above now)")
    else:
        lo_lbl, mid_lbl, hi_lbl = (f"Falls ≥{thr}%", f"Stays (±{thr}%)", f"Rises ≥{thr}%")

    # rb = rp − rf isolates the basis move not explained by FX; "unexplained
    # fraction" = share of premium variance from that basis; corr is FX coupling.
    rp = np.diff(np.log1p(p))
    rf = np.diff(np.log(F))
    rb = rp - rf                               # residual basis daily move (exact)
    vp = float(np.var(rp))
    if vp > 0:
        unexplained = float(np.var(rb) / vp)   # premium variance from the basis (ex-FX)
        corr = float(np.corrcoef(rp, rf)[0, 1]) if rf.std() > 0 else 0.0
    else:
        unexplained, corr = 1.0, 0.0
    unexplained = max(0.0, min(1.0, unexplained))
    r2 = corr ** 2                             # premium/FX scatter R² (coupling)
    resid = rb                                 # basis series for the chart (premium ex-FX)

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
        "r2_with_premium": round(r2, 4),        # premium/FX scatter R² (coupling)
        "corr_with_premium": round(corr, 4),
        "unexplained_frac": round(unexplained, 4),   # premium variance NOT explained by FX (basis)
        "explained_frac": round(1.0 - unexplained, 4),
    }

    return {
        "stock_code": stock_code,
        "horizon_days": int(horizon_days),
        "horizon_trading": int(k),
        "current_premium": p0,
        "current_premium_live": live_now,   # True = intraday snapshot, False = last daily close
        "current_regime": cur,
        "current_regime_label": cur_label,
        "conditional": conditional,   # False = fell back to unconditional
        "band_pct": round(band * 100, 1) if band is not None else None,
        "n_samples": int(len(ends_cond)),
        "regimes": [
            bucket(lo, lo_lbl),
            bucket(mid, mid_lbl),
            bucket(hi, hi_lbl),
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


def _adf_test(series):
    """Augmented Dickey-Fuller unit-root test + AR(1) half-life on the premium
    LEVEL series. ADF null = unit root (random walk, NOT mean-reverting); a low
    p-value (< 0.05) rejects it => the premium is stationary / mean-reverting,
    which is the whole thesis of the ADR-basis trade. Half-life = trading days
    for a premium deviation to decay halfway back to its mean.

    Returns all fields as None (with a `reason`) for a degenerate/short series
    rather than raising, mirroring how the regime stats fall back to 0.0.
    """
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    out = {"stat": None, "pvalue": None, "nobs": int(len(x)), "usedlag": None,
           "crit": {}, "half_life": None, "stationary": None, "reason": None}
    if len(x) < 30:
        out["reason"] = "too few observations (need >= 30)"
        return out
    try:
        stat, pval, usedlag, nobs, crit, _ = adfuller(x, autolag="AIC")
    except Exception as e:
        out["reason"] = f"adfuller failed: {e}"
        return out
    out.update({"stat": float(stat), "pvalue": float(pval), "usedlag": int(usedlag),
                "nobs": int(nobs), "crit": {k: float(v) for k, v in crit.items()},
                "stationary": bool(pval < 0.05)})
    # AR(1) half-life via OLS of the level on its own lag: p_t = a + phi*p_{t-1}.
    # Mean-reversion requires 0 < phi < 1; then half_life = -ln2 / ln(phi).
    # phi <= 0 or phi >= 1 => not mean-reverting => half_life stays None.
    phi = float(np.polyfit(x[:-1], x[1:], 1)[0])
    if 0.0 < phi < 1.0:
        out["half_life"] = float(-np.log(2.0) / np.log(phi))
    return out


def adr_premium_stats(stock_code, period="3y"):
    """Historical ADR-vs-local premium series + regime probabilities.

    premium_t = (adr_usd_t / adr_ratio * fx_t) / tw_close_t - 1

    Returns dates, premium %, the latest premium, and 3 regimes (high / mid /
    low) with their historical frequency and conditional-mean premium — the
    inputs to the Expected-Value calc in the trade modal.
    """
    cfg = _adr_map()[stock_code]
    key = (stock_code, period)

    def _with_live(stats):
        # History (dates/series/regimes) is cached; the *latest* premium is
        # refreshed to an intraday snapshot each call so the badge isn't stale.
        live = _live_premium_fx(stock_code)
        if live is None:
            return {**stats, "latest_premium_live": False}
        return {**stats, "latest_premium": live[0], "latest_premium_live": True}

    hit = _prem_cache.get(key)
    if hit and time.time() - hit[0] < _PREM_TTL:
        return _with_live(hit[1])

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
        "adf": _adf_test(prem.values),          # unit-root test: is the premium mean-reverting?
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
    return _with_live(stats)


def _last_price(ticker: str) -> float:
    tk = yf.Ticker(ticker)
    # fast_info.last_price is the freshest single snapshot Yahoo exposes
    # (intraday during market hours, ~15 min delayed on the free feed).
    try:
        p = float(tk.fast_info["last_price"])
        if p > 0:
            return p
    except Exception:
        pass
    # Fallback order: freshest intraday 1-minute bar, then daily close. Prefer
    # the 1-minute bar so we never silently drop back to a stale prior close.
    for period, interval in (("1d", "1m"), ("5d", "1d")):
        try:
            hist = tk.history(period=period, interval=interval)
            s = hist["Close"].dropna() if not hist.empty else None
            if s is not None and len(s):
                return float(s.iloc[-1])
        except Exception:
            continue
    raise RuntimeError(f"no price for {ticker}")


def _live_premium_fx(stock_code):
    """Intraday snapshot of (ADR premium, FX) for 'now', bypassing the daily
    history. premium = (adr_usd/adr_ratio * fx) / tw_share - 1, all from the
    freshest available Yahoo snapshot. Returns (premium, fx) or None on failure
    so callers can fall back to the last daily close.
    """
    cfg = _adr_map().get(stock_code)
    if not cfg:
        return None
    try:
        adr = _last_price(cfg["adr_ticker"])          # USD per ADR
        fx = _last_price(cfg["fx_ticker"])            # TWD per USD
        tw = _last_price(f"{stock_code}.TW")          # TWD per Taiwan share
        if adr > 0 and fx > 0 and tw > 0:
            prem = (adr / cfg["adr_ratio"] * fx) / tw - 1.0
            return float(prem), float(fx)
    except Exception:
        return None
    return None


def read_us_option(stock_codes, option_type="All", min_days=1, max_days=365,
                     compute_iv=True, keep_noniv=False):
    """Return (df, error_or_None, meta) of UMC options priced in TWD per Taiwan
    share, for one or more stock codes. Never raises — mirrors read_warrant /
    read_tw_option's contract.

    Columns mirror options_logic.read_tw_option so the same matching code can
    consume it: type, strike, days_to_expiry, bid, ask, iv_bid, iv_ask,
    contract, is_live, plus USD reference fields (strike_usd, fx, adr_price).
    When multiple codes are passed, each row also carries a "stock_code" column
    so callers can tell the legs apart (mirrors the Supabase snapshot schema).
    """
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    # Snapshot-first read (MARKET_SOURCE=supabase). The stored snapshot is the
    # superset-with-IV; compute_iv=True (scanner) drops non-converged-IV rows,
    # compute_iv=False (arb) keeps the superset. Empty snapshot / read error
    # falls through to the live path below; the supabase path never warms _cache.
    if db_market.snapshot_enabled():
        snap, as_of = None, None
        try:
            snap, as_of = db_market.read_snapshot("us_options", codes=list(stock_codes))
        except Exception as e:
            applog.log("USOPT", f"supabase read failed ({e}) — falling back to live")
            snap = None
        if snap is not None and not snap.empty:
            meta = {"as_of": as_of, "cached": True}
            if compute_iv:
                snap = snap[snap["iv_ask"].notna()]
            else:
                # Live compute_iv=False emits NaN IV-derived metrics; blank the
                # superset's stored values so the arb path (which branches on IV
                # presence) matches the live arb frame exactly.
                snap = snap.copy()
                for _c in ("iv_ask", "iv_bid", "delta_calc"):
                    if _c in snap.columns:
                        snap[_c] = np.nan
            result = _apply_us_option_filters(snap, option_type, min_days, max_days)
            if result.empty:
                return pd.DataFrame(), "No US options in range", meta
            return result, None, meta

    # scrape_yfinance_us_option already flags which of its failure modes is a
    # genuine fetch failure (meta["hard_error"]); trust that rather than
    # re-deriving it from the message text here.
    dfs, skip_reasons, hard_errors, as_ofs = [], [], [], []
    for code in stock_codes:
        df, err, meta = scrape_yfinance_us_option(
            code, option_type, min_days, max_days, compute_iv, keep_noniv,
        )
        if err:
            if meta.get("hard_error"):
                hard_errors.append(f"{code}: {err}")
            else:
                skip_reasons.append(f"{code}: {err}")
        if df is not None and not df.empty:
            dfs.append(df.assign(stock_code=code))
        if meta.get("as_of"):
            as_ofs.append(meta["as_of"])
    combined_meta = {
        "as_of": min(as_ofs) if as_ofs else None,
        "cached": False,
        "hard_error": "; ".join(hard_errors) if hard_errors else None,
    }
    if not dfs:
        err_msg = "; ".join(hard_errors or skip_reasons) or "No US options in range"
        return pd.DataFrame(), err_msg, combined_meta
    result = pd.concat(dfs, ignore_index=True)
    return result, None, combined_meta


def scrape_yfinance_us_option(stock_code, option_type="All", min_days=1, max_days=365,
                          compute_iv=True, keep_noniv=False):
    """Pure live-scrape reader (no Supabase snapshot branch).

    The scraper half of read_us_option: always walks the yfinance option
    chain fresh (never short-circuits on the in-process _cache TTL) so a manual
    refresh is guaranteed live, then repopulates _cache so the reader-side
    cache-hit path stays warm. Never raises — returns (df, error_or_None, meta),
    mirroring warrant_logic.scrape_cmoney_warrant / options_logic.scrape_tw_option.
    """
    # meta["hard_error"] marks a genuine yfinance/price failure, as opposed to
    # "this code has no chain / nothing in range", which is a normal empty scan.
    if stock_code not in _adr_map():
        return (pd.DataFrame(), f"{stock_code}: no US ADR mapping",
                {"as_of": None, "cached": False, "hard_error": None})

    cfg = _adr_map()[stock_code]
    cache_key = (stock_code, compute_iv, keep_noniv)

    t0 = time.time()
    adr_ratio = cfg["adr_ratio"]             # ordinary shares per ADR
    try:
        adr = _last_price(cfg["adr_ticker"])     # USD per ADR
        fx = _last_price(cfg["fx_ticker"])       # TWD per USD
    except Exception as e:
        applog.log("USOPT", f"{stock_code} price fetch failed: {e}", level="ERROR")
        return (pd.DataFrame(), str(e),
                {"as_of": None, "cached": False, "hard_error": str(e)})

    meta = {
        "as_of": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
        "cached": False,
        "hard_error": None,
    }

    if adr <= 0 or fx <= 0:
        # No usable ADR/FX print at all — the upstream feed is broken, not a
        # chain that happens to be empty.
        applog.log("USOPT", f"{stock_code} bad ADR price ({adr}) or FX ({fx})", level="ERROR")
        return pd.DataFrame(), "bad ADR price or FX", {**meta, "hard_error": "bad ADR price or FX"}

    # Underlying value expressed per Taiwan share, in TWD — the same basis the
    # warrant leg uses (so strike_diff_pct etc. are apples-to-apples).
    S_twd = adr / adr_ratio * fx

    tk = yf.Ticker(cfg["adr_ticker"])
    expiries = tk.options
    if not expiries:
        applog.log("USOPT", f"{stock_code} {cfg['adr_ticker']} no option expiries")
        return pd.DataFrame(), f"{cfg['adr_ticker']}: no option expiries", meta

    # One yfinance round-trip per expiry, so this walk is the slow part; logging
    # before it starts makes a hang attributable to this stage.
    applog.log(
        "USOPT",
        f"{stock_code} {cfg['adr_ticker']} walking {len(expiries)} expiries "
        f"(adr={adr} fx={fx})",
    )

    today = pd.Timestamp.now().normalize()
    rows = []
    failed_exp = 0

    # Compute dte per expiry and drop dte<1 BEFORE fetching so dead-on-arrival
    # expiries never spend a round-trip. Keep the survivors in the original
    # expiries order — the pre-sort frame must be assembled in that order so the
    # output is byte-identical to the sequential version (the final
    # sort_values is stable, so identical tie order is preserved).
    fetchable = []
    for exp in expiries:
        exp_ts = pd.Timestamp(exp)
        dte = int((exp_ts - today).days)
        if dte < 1:
            continue
        fetchable.append((exp, exp_ts, dte))

    # Each expiry is an independent HTTPS GET; a small pool overlaps them.
    # max_workers=5 stays polite to Yahoo; results are reordered to `fetchable`
    # order below for deterministic output.
    def _fetch(exp):
        try:
            return tk.option_chain(exp)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        chains = list(pool.map(_fetch, [exp for exp, _ts, _dte in fetchable]))

    conv = fx / adr_ratio  # USD/ADR -> TWD/Taiwan-share
    for (exp, exp_ts, dte), chain in zip(fetchable, chains):
        if chain is None:
            failed_exp += 1
            continue

        T = dte / 365.0
        for is_put, leg in ((False, chain.calls), (True, chain.puts)):
            opt_type = "Put" if is_put else "Call"
            if leg is None or len(leg) == 0:
                continue

            # Vectorized replacement for the per-leg iterrows() loop. yfinance
            # returns each leg as a DataFrame already, so pull the columns into
            # numpy arrays and process the whole leg at once.
            def _col(name):
                if name in leg.columns:
                    return pd.to_numeric(leg[name], errors="coerce").to_numpy(dtype=float)
                return np.full(len(leg), np.nan)

            K_usd = _col("strike")
            with np.errstate(all="ignore"):
                # float(x or np.nan): 0 -> NaN, everything else unchanged.
                bid_usd = np.where(_col("bid") == 0, np.nan, _col("bid"))
                ask_usd = np.where(_col("ask") == 0, np.nan, _col("ask"))
                last_usd = np.where(_col("lastPrice") == 0, np.nan, _col("lastPrice"))

            vol_raw = leg["volume"].to_numpy() if "volume" in leg.columns \
                else np.full(len(leg), np.nan)
            oi_raw = leg["openInterest"].to_numpy() if "openInterest" in leg.columns \
                else np.full(len(leg), np.nan)

            with np.errstate(all="ignore"):
                kok = np.isfinite(K_usd) & (K_usd > 0)
                ask_live = np.isfinite(ask_usd) & (ask_usd > 0)
                bid_live = np.isfinite(bid_usd) & (bid_usd > 0)
                is_live = ask_live & bid_live
                a_usd = np.where(ask_live, ask_usd, last_usd)
                b_usd = np.where(bid_live, bid_usd, last_usd)
                aok = np.isfinite(a_usd) & (a_usd > 0)
            pre = kok & aok

            m = len(leg)
            adr_arr = np.full(m, float(adr))
            T_arr = np.full(m, T)
            if compute_iv:
                # IV in native USD/ADR space (scale-free).
                iv_ask = implied_vol_vec(a_usd, adr_arr, K_usd, T_arr, R_US, 1.0, is_put)
                with np.errstate(all="ignore"):
                    b_for = np.where(np.isfinite(b_usd) & (b_usd > 0), b_usd, np.nan)
                iv_bid = implied_vol_vec(b_for, adr_arr, K_usd, T_arr, R_US, 1.0, is_put)
                ask_conv = ~np.isnan(iv_ask)
                fbk = np.isnan(iv_ask) & ~np.isnan(iv_bid)
                iv_ask = np.where(fbk, iv_bid, iv_ask)
                # Delta boundary refinement (no leverage column here); re-solve
                # on the same USD price the scalar path used for iv_ask.
                price_for_ivask = np.where(ask_conv, a_usd, b_for)
                iv_ask = _refine_iv_for_rounding(
                    iv_ask, price_for_ivask, adr_arr, K_usd, T_arr, R_US, 1.0, is_put,
                    check_delta=True,
                )
                converged = ~np.isnan(iv_ask)
                delta = bs_delta_vec(adr_arr, K_usd, T_arr, R_US, iv_ask, 1.0, is_put)
                iv_bid = np.where(converged, iv_bid, np.nan)
                delta = np.where(converged, delta, np.nan)
                iv_keep = converged if not keep_noniv else np.ones(m, dtype=bool)
            else:
                # Arb finder does not use IV/delta — skip the solve.
                iv_ask = iv_bid = delta = np.full(m, np.nan)
                iv_keep = np.ones(m, dtype=bool)

            keep = pre & iv_keep
            exp_lbl = exp_ts.strftime("%b%y")
            for j in range(m):
                if not keep[j]:
                    continue
                Kx = K_usd[j]; ax = a_usd[j]; bx = b_usd[j]
                bok = np.isfinite(bx) and bx > 0
                strike_twd = Kx * conv
                ask_twd = ax * conv
                bid_twd = (bx * conv) if bok else np.nan
                ivb = iv_bid[j]
                contract = f"{'P' if is_put else 'C'}{float(Kx):g} {exp_lbl} (US)"
                rows.append({
                    "contract": contract,
                    "type": opt_type,
                    "underlying_price": round(S_twd, 4),
                    "strike": round(strike_twd, 4),
                    "days_to_expiry": dte,
                    "bid": round(bid_twd, 4) if np.isfinite(bid_twd) else None,
                    "ask": round(ask_twd, 4),
                    "iv_ask": round(float(iv_ask[j]), 4),
                    "iv_bid": round(float(ivb), 4) if np.isfinite(ivb) else None,
                    "delta_calc": round(float(delta[j]), 4),
                    "volume": int(vol_raw[j]) if pd.notna(vol_raw[j]) else 0,
                    "oi": int(oi_raw[j]) if pd.notna(oi_raw[j]) else 0,
                    "is_live": bool(is_live[j]),
                    "ask_live": bool(ask_live[j]),
                    "bid_live": bool(bid_live[j]),
                    # USD reference (for display / auditing)
                    "strike_usd": round(float(Kx), 4),
                    "bid_usd": round(float(bx), 4) if bok else None,
                    "ask_usd": round(float(ax), 4),
                    "adr_price": round(adr, 4),
                    "fx": round(fx, 4),
                })

    if not rows:
        applog.log(
            "USOPT",
            f"{stock_code} {cfg['adr_ticker']} -> 0 contracts from "
            f"{len(expiries)} expiries in {time.time() - t0:.1f}s",
        )
        return pd.DataFrame(), "no US options in range", meta

    applog.log(
        "USOPT",
        f"{stock_code} {cfg['adr_ticker']} fetched {len(rows)} contracts from "
        f"{len(expiries)} expiries in {time.time() - t0:.1f}s"
        + (f" ({failed_exp} expiries failed)" if failed_exp else ""),
    )
    df = pd.DataFrame(rows).sort_values(["days_to_expiry", "strike"]).reset_index(drop=True)
    _cache.set(cache_key, df.copy())
    filtered = _apply_us_option_filters(df, option_type, min_days, max_days)
    if filtered.empty:
        return pd.DataFrame(), "no US options in range", meta
    return filtered, None, meta


def _apply_us_option_filters(df, option_type, min_days, max_days):
    """Pure filtering (day-range + type). Returns the filtered df, possibly
    empty. No raise — callers own the empty-case messaging, matching
    options_logic._apply_option_filters / warrant_logic._apply_warrant_filters."""
    view = df[df["days_to_expiry"].between(int(min_days), int(max_days))]
    if option_type != "All":
        view = view[view["type"] == option_type]
    return view.copy().reset_index(drop=True)


def us_option_last(us_code, opt_type, strike_twd, expiry_iso, fx, adr_ratio):
    """Last traded price (USD per ADR) of the surviving US option leg.

    Matches the ADR option chain by nearest expiry then nearest strike. The
    stored strike is TWD-per-TW-share, so it is converted back to a USD/ADR
    strike (× adr_ratio ÷ FX) before matching. Returns None if anything is
    missing so the caller can fall back to a model value.
    """
    cfg = _adr_map().get(us_code)
    if not cfg or not fx or not adr_ratio or strike_twd <= 0:
        return None
    strike_usd = strike_twd * adr_ratio / fx
    tk = yf.Ticker(cfg["adr_ticker"])
    exps = tk.options
    if not exps:
        return None
    if expiry_iso:
        target = pd.Timestamp(expiry_iso).normalize()
        exp = min(exps, key=lambda e: abs((pd.Timestamp(e) - target).days))
    else:
        exp = exps[0]
    chain = tk.option_chain(exp)
    leg = chain.calls if str(opt_type).lower().startswith("c") else chain.puts
    if leg is None or leg.empty:
        return None
    row = leg.iloc[(leg["strike"] - strike_usd).abs().argmin()]
    last = float(row.get("lastPrice") or 0)
    return last if last > 0 else None


def us_options_scan(stock_codes, option_type, min_days, max_days, min_volume):
    """American (US ADR) option chains for the scanner, in native USD.

    Wraps read_us_option (which also carries the TWD-normalized view used by
    the arb finder) and returns the USD-native columns a US options trader
    expects: strike_usd, bid_usd, ask_usd, adr_price (underlying), plus IV /
    delta / volume / OI. Returns (df, error_or_None, meta) — same contract as
    the read_X functions it wraps.
    """
    df, error, meta = read_us_option(
        stock_codes, option_type, min_days=max(1, int(min_days)),
        max_days=int(max_days), compute_iv=True,
    )
    if df.empty:
        return pd.DataFrame(), error or "No data returned", meta
    if int(min_volume) > 0:
        df = df[df["volume"] >= int(min_volume)]
    if df.empty:
        return pd.DataFrame(), "No data returned", meta
    if "stock_code" in df.columns:
        df = df.rename(columns={"stock_code": "product"})
    else:
        df = df.assign(product=stock_codes[0] if len(stock_codes) == 1 else None)
    cols = ["product", "contract", "type", "strike_usd", "adr_price",
            "days_to_expiry", "bid_usd", "ask_usd", "iv_ask", "iv_bid",
            "delta_calc", "volume", "oi", "fx", "is_live", "ask_live", "bid_live"]
    return df[[c for c in cols if c in df.columns]], None, meta
