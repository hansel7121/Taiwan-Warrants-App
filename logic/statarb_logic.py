"""TSMC underlying simulation and pair scanner for the Arb Finder → StatArb sub-tab (port of tsmc_2330_underlying_sim.ipynb).

`compute(px_all)` turns a dividend-adjusted close series into every chart the tab draws: EWMA vol, the standardised
shock pool, GARCH vol forecast, and 10,000 simulated paths from a filtered block bootstrap and from GBM fed the same
vol path, the bootstrap again with the historical drift added back, plus a two-sample t-test between bootstrap and GBM.
`state()` serves the cached result; `update()` refetches and rebuilds. `scan()` prices every long-warrant / short-option
pair on 2330 against the cached bootstrap paths (`scan_pairs` is the pure core), with the warrant leg rounded down and up
to whole board lots as two separate trades: pure arbs, and for the rest the share of paths that reach the loss region
under zero drift, historical drift, and both again with vol × VOL_STRESS. `scan_lp()` runs logic/statarb_lp.py's
whole-lot MILP per option expiry over the whole chain against the same path sets (#9).
"""
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy import stats

TICKER = "2330.TW"
LOOKBACK_YEARS = 5
LAM = 0.94          # EWMA decay (RiskMetrics)
BLOCK = 5           # bootstrap block length, trading days
N_MAX = 120         # longest simulated horizon, trading days
N_SIMS = 10_000
TD_YEAR = 245       # TWSE trading days per year
HORIZONS = [5, 20, 60, 120]
N_SAMPLE_PATHS = 15
FAN_QS = [1, 5, 25, 50, 75, 95, 99]
TW_TZ = ZoneInfo("Asia/Taipei")
CLOSE_HOUR = 14     # a row dated today before this hour (TPE) is a live intraday price, not a close
STOCK_CODE = "2330"
VOL_STRESS = 1.2    # scanner's stressed scenarios scale every simulated daily return by this
GRID_POINTS = 6000  # spot grid (0 to 3x spot) for each pair's expiry PnL

_cache = None
_sim = None         # bootstrap paths kept server-side for the scanner, never sent to the browser
_lock = threading.Lock()


def fetch_prices(now=None):
    """Download ~12y of dividend-adjusted 2330 closes; drop today's row while the session is still open."""
    import yfinance as yf
    raw = yf.download(TICKER, period="12y", auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"yfinance returned no data for {TICKER}")
    close = raw["Close"]
    px = (close[TICKER] if isinstance(close, pd.DataFrame) else close).dropna()
    px.index = pd.to_datetime(px.index).tz_localize(None)
    now = now or datetime.now(TW_TZ)
    if len(px) and px.index[-1].date() == now.date() and now.hour < CLOSE_HOUR:
        px = px.iloc[:-1]
    return px


def ewma_vol(r, lam=LAM, seed_n=20):
    """EWMA vol for each day using only returns before it."""
    v = np.empty_like(r)
    v[0] = r[:seed_n].var()
    for t in range(1, len(r)):
        v[t] = lam * v[t - 1] + (1 - lam) * r[t - 1] ** 2
    return np.sqrt(v)


def fit_garch(r):
    """GARCH(1,1) Student-t on demeaned returns -> (phi, long-run daily vol), with the notebook's unit-root guard."""
    from arch import arch_model
    res = arch_model(r * 100, mean="Zero", vol="GARCH", p=1, q=1, dist="t").fit(disp="off")
    omega, alpha, beta = res.params["omega"], res.params["alpha[1]"], res.params["beta[1]"]
    phi = float(alpha + beta)
    sig_lr = float(np.sqrt(omega / (1 - phi)) / 100) if phi < 1 else float("nan")
    if phi > 0.995 or not np.isfinite(sig_lr):
        return 0.98, float(r.std()), True
    return phi, sig_lr, False


def block_bootstrap(z, n_sims, n_days, block, rng):
    """Resample z in blocks of consecutive days -> (n_sims, n_days) unit-vol shocks."""
    n_blocks = -(-n_days // block)
    starts = rng.integers(0, len(z) - block + 1, size=(n_sims, n_blocks))
    return z[starts[..., None] + np.arange(block)].reshape(n_sims, -1)[:, :n_days]


def _r(a, d=6):
    """Round a float or array for JSON."""
    if np.isscalar(a):
        return None if not np.isfinite(a) else round(float(a), d)
    return np.round(np.asarray(a, dtype=float), d).tolist()


def _fan(cum, s0):
    """Percentile bands of simulated price per day, plus a few sample paths."""
    paths = s0 * np.exp(cum)
    bands = np.percentile(paths, FAN_QS, axis=0)
    return {"bands": {f"p{q}": _r(b, 2) for q, b in zip(FAN_QS, bands)},
            "samples": [_r(p, 2) for p in paths[:N_SAMPLE_PATHS]]}


def _dist(x, bins, grid):
    """Histogram density, KDE and summary stats of one sample (in %)."""
    dens, edges = np.histogram(x, bins=bins, density=True)
    return {"centers": _r((edges[:-1] + edges[1:]) / 2, 4), "density": _r(dens, 6),
            "kde": _r(stats.gaussian_kde(x)(grid), 6),
            "mean": _r(x.mean()), "median": _r(np.median(x)), "std": _r(x.std())}


def compute(px_all, seed=42):
    """Build every StatArb chart series from a dividend-adjusted close series."""
    return _build(px_all, seed)[0]


def _build(px_all, seed=42):
    """(chart payload, simulated paths for the scanner) from a dividend-adjusted close series."""
    px = px_all[px_all.index >= px_all.index[-1] - pd.DateOffset(years=LOOKBACK_YEARS)]
    s0 = float(px.iloc[-1])
    r_raw = np.log(px).diff().dropna()
    mu = float(r_raw.mean())
    r = (r_raw - mu).values
    dates = r_raw.index
    ann = np.sqrt(TD_YEAR)

    sig = ewma_vol(r)
    z_raw = r / sig
    z = z_raw / z_raw.std()
    z_c = z - z.mean()          # centred pool for simulation, so it carries no hidden drift

    sig_today = float(np.sqrt(LAM * sig[-1] ** 2 + (1 - LAM) * r[-1] ** 2))
    sig_sample = float(r.std())
    phi, sig_lr, garch_fallback = fit_garch(r)

    k = np.arange(N_MAX)
    sig_path = np.sqrt(sig_lr ** 2 + phi ** k * (sig_today ** 2 - sig_lr ** 2))
    avg_vol = np.sqrt(np.cumsum(sig_path ** 2) / (k + 1))

    boot = (block_bootstrap(z_c, N_SIMS, N_MAX, BLOCK, np.random.default_rng(seed)) * sig_path).cumsum(axis=1)
    gbm = (np.random.default_rng(seed + 1).standard_normal((N_SIMS, N_MAX)) * sig_path).cumsum(axis=1)
    boot_drift = boot + mu * (k + 1)    # same paths with the historical daily drift added back

    dens, edges = np.histogram(z, bins=80, density=True)
    xs = np.linspace(-6, 6, 241)
    (theo, ordered), (slope, intercept, _) = stats.probplot(z, dist="norm")

    ttest = []
    for h in HORIZONS:
        x, y = boot[:, h - 1] * 100, gbm[:, h - 1] * 100
        lo, hi = np.percentile(np.r_[x, y], [0.2, 99.8])
        bins, grid = np.linspace(lo, hi, 81), np.linspace(lo, hi, 200)
        t = stats.ttest_ind(x, y, equal_var=False)
        ttest.append({"h": h, "grid": _r(grid, 4), "boot": _dist(x, bins, grid), "gbm": _dist(y, bins, grid),
                      "dmean": _r(x.mean() - y.mean()), "t": _r(t.statistic, 4), "p": _r(t.pvalue, 4),
                      "levene_p": _r(stats.levene(x, y).pvalue, 4)})

    sim = {"boot": boot.astype(np.float32), "mu": mu, "asof": px.index[-1].date(), "s0": s0}
    return {
        "ticker": TICKER, "asof": str(px.index[-1].date()), "s0": s0,
        "computed_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        "params": {"lookback_years": LOOKBACK_YEARS, "lam": LAM, "block": BLOCK, "n_max": N_MAX,
                   "n_sims": N_SIMS, "td_year": TD_YEAR},
        "kpis": {"n_days": int(len(r)), "drift_ann": _r(mu * TD_YEAR), "sig_today_ann": _r(sig_today * ann),
                 "sig_sample_ann": _r(sig_sample * ann), "sig_lr_ann": _r(sig_lr * ann),
                 "pct_today": _r(stats.percentileofscore(sig, sig_today), 1), "phi": _r(phi, 4),
                 "half_life": _r(np.log(0.5) / np.log(phi), 1), "garch_fallback": garch_fallback,
                 "z_kurt": _r(stats.kurtosis(z, fisher=False), 3), "z_skew": _r(stats.skew(z), 3),
                 "z_tail_days": int((np.abs(z) > 3).sum()), "z_tail_normal": _r(0.0027 * len(z), 1)},
        "ewma": {"dates": [d.strftime("%Y-%m-%d") for d in dates], "abs_ann": _r(np.abs(r) * ann, 4),
                 "ewma_ann": _r(sig * ann, 4)},
        "zhist": {"centers": _r((edges[:-1] + edges[1:]) / 2, 4), "density": _r(dens, 6),
                  "normal_x": _r(xs, 3), "normal_pdf": _r(stats.norm.pdf(xs), 8)},
        "qq": {"theo": _r(theo, 4), "ordered": _r(ordered, 4), "slope": _r(slope), "intercept": _r(intercept)},
        "volhist": {"values": _r(sig * ann, 4), "today": _r(sig_today * ann), "sample": _r(sig_sample * ann)},
        "forecast": {"days": (k + 1).tolist(), "sig_path_ann": _r(sig_path * ann), "avg_ann": _r(avg_vol * ann),
                     "today_ann": _r(sig_today * ann), "lr_ann": _r(sig_lr * ann)},
        "fan": {"days": (k + 1).tolist(), "boot": _fan(boot, s0), "gbm": _fan(gbm, s0),
                "boot_drift": _fan(boot_drift, s0)},
        "ttest": ttest,
    }, sim


def update():
    """Refetch prices and rebuild the cache; one rebuild at a time."""
    global _cache, _sim
    with _lock:
        _cache, _sim = _build(fetch_prices())
        return _cache


def state():
    """Cached result, built on first use."""
    return _cache if _cache is not None else update()


def scenarios(sim, vol_stress=VOL_STRESS):
    """The four scanner path sets as cumulative log returns, keyed by name."""
    boot = sim["boot"].astype(np.float64)
    drift = sim["mu"] * np.arange(1, boot.shape[1] + 1)
    return {"zero": boot, "drift": boot + drift, "stress": vol_stress * boot, "stress_drift": vol_stress * boot + drift}


def _loss_intervals(grid, pnl):
    """[(lo, hi)] spot intervals where pnl < 0; hi is inf when the loss runs off the top of the grid."""
    loss = np.r_[False, pnl < 0, False]
    edges = np.flatnonzero(np.diff(loss.astype(int)))
    step = grid[1] - grid[0]
    out = []
    for a, b in zip(edges[::2], edges[1::2]):
        lo = 0.0 if a == 0 else grid[a] - step / 2
        hi = np.inf if b == len(grid) else grid[b - 1] + step / 2
        out.append((float(lo), float(hi)))
    return out


def _quote(v):
    """A positive quote as float, else None (missing or zero side)."""
    return float(v) if v is not None and np.isfinite(v) and v > 0 else None


def scan_pairs(warrant_df, opt_df, contract_size, sim, spot, today, vol_stress=VOL_STRESS):
    """Every long-warrant / short-option pair with a net credit, sized down and up to whole lots, with loss region and P(loss)."""
    w = warrant_df[(warrant_df["ask"] > 0) & (warrant_df["exercise_ratio"] > 0)]
    o = opt_df[opt_df["bid_live"] & (opt_df["bid"] > 0)]
    if "ask" in o.columns:
        o = o[~((o["ask"] > 0) & (o["bid"] > o["ask"]))]      # crossed quote = stale, its bid isn't really there
    grid = np.linspace(spot * 3 / GRID_POINTS, spot * 3, GRID_POINTS)
    first_day = np.datetime64(sim["asof"]) + np.timedelta64(1, "D")
    paths = {k: spot * np.exp(c) for k, c in scenarios(sim, vol_stress).items()}
    n_max = sim["boot"].shape[1]
    lo_hi = {}      # (scenario, N) -> (running min, running max, price at N), filled on demand

    def path_stats(k, n):
        if (k, n) not in lo_hi:
            px = paths[k][:, :n]
            lo_hi[(k, n)] = (np.minimum(px.min(axis=1), spot), np.maximum(px.max(axis=1), spot), px[:, -1])
        return lo_hi[(k, n)]

    rows = []
    for ww in w.itertuples(index=False):
        ratio = float(ww.exercise_ratio)
        exact_lots = round(contract_size / ratio / 1000, 6)
        lo_lots, hi_lots = int(np.floor(exact_lots)), int(np.ceil(exact_lots))
        sizings = [("exact", lo_lots)] if lo_lots == hi_lots else [("down", lo_lots), ("up", hi_lots)]
        call = ww.type == "Call"
        iw = np.maximum(grid - ww.strike, 0) if call else np.maximum(ww.strike - grid, 0)
        for oo in o.itertuples(index=False):
            if oo.type != ww.type or oo.days_to_expiry > ww.days_to_expiry:
                continue
            diff_ps = round(float(oo.bid) - float(ww.ask) / ratio, 4)     # Direct Match's price_diff
            if diff_ps <= 0:
                continue
            io = np.maximum(grid - oo.strike, 0) if call else np.maximum(oo.strike - grid, 0)
            expiry = np.datetime64(today) + np.timedelta64(int(oo.days_to_expiry), "D")
            n = int(np.busday_count(first_day, expiry + np.timedelta64(1, "D")))
            for rounding, lots in sizings:
                if lots < 1:
                    continue
                n_w = lots * 1000
                credit = float(oo.bid) * contract_size - float(ww.ask) * n_w
                if credit <= 0:
                    continue
                pnl = credit + n_w * ratio * iw - contract_size * io      # at option expiry, warrant at intrinsic
                ivs = _loss_intervals(grid, pnl)
                row = {"warrant_code": ww.warrant_code, "warrant_name": ww.warrant_name, "option_contract": oo.contract,
                       "type": ww.type, "warrant_strike": round(float(ww.strike), 2), "opt_strike": round(float(oo.strike), 2),
                       "warrant_dte": int(ww.days_to_expiry), "opt_dte": int(oo.days_to_expiry), "trading_days": n,
                       "exercise_ratio": ratio, "opt_contract_size": int(contract_size),
                       "rounding": rounding, "exact_lots": exact_lots, "lots": lots,
                       "warrant_depth_lots": int(ww.ask_qty or 0), "fillable": int(ww.ask_qty or 0) >= lots,
                       "warrant_ask": float(ww.ask), "warrant_bid": _quote(getattr(ww, "bid", None)),
                       "opt_bid": float(oo.bid), "opt_ask": _quote(getattr(oo, "ask", None)), "price_diff": diff_ps,
                       "credit": round(credit), "max_loss": round(min(float(pnl.min()), 0.0)),
                       "pure": not ivs, "loss_region": [[a, None if not np.isfinite(b) else b] for a, b in ivs],
                       "dist_to_loss_pct": None, "testable": bool(ivs) and 1 <= n <= n_max,
                       "p_touch": None, "p_expiry": None}
                if ivs:
                    row["dist_to_loss_pct"] = round(100 * min(0.0 if a <= spot <= b else min(abs(a - spot), abs(b - spot))
                                                              for a, b in ivs) / spot, 2)
                if row["testable"]:
                    row["p_touch"], row["p_expiry"] = {}, {}
                    for k in paths:
                        lo, hi, end = path_stats(k, n)
                        touch = np.zeros(len(lo), bool)
                        at_end = np.zeros(len(lo), bool)
                        for a, b in ivs:
                            touch |= (lo < b) & (hi > a)
                            at_end |= (end > a) & (end < b)
                        row["p_touch"][k] = round(float(touch.mean()), 6)
                        row["p_expiry"][k] = round(float(at_end.mean()), 6)
                rows.append(row)
    return rows


def scan(vol_stress=VOL_STRESS, now=None):
    """Fetch 2330 warrants and options and score every pair against the cached simulation."""
    if not 1.0 <= vol_stress <= 3.0:
        raise ValueError(f"vol_stress must be between 1.0 and 3.0, got {vol_stress}")
    from logic import options_logic, warrant_logic
    state()
    now = now or datetime.now(TW_TZ)
    w, w_err, w_meta = warrant_logic.read_warrant([STOCK_CODE], "All", 0, 365, 0, 1e9, 0, compute_iv=False)
    o, o_err, o_meta = options_logic.read_tw_option([STOCK_CODE], "All", min_days=1, compute_iv=False)
    if w.empty or o.empty:
        raise RuntimeError(f"no data: warrants {w_err or len(w)}, options {o_err or len(o)}")
    spot = float(w["underlying_price"].iloc[0])
    contract_size = options_logic._commodity_map()[STOCK_CODE]["exercise_ratio"]
    rows = scan_pairs(w, o, contract_size, _sim, spot, now.date(), vol_stress)
    rows.sort(key=lambda r: (not r["pure"], max(r["p_touch"].values()) if r["p_touch"] else 2, -r["credit"]))
    return {"spot": spot, "sim_asof": str(_sim["asof"]), "quotes_as_of": (w_meta or {}).get("as_of"),
            "scanned_at": now.isoformat(timespec="seconds"), "vol_stress": vol_stress, "n_sims": int(_sim["boot"].shape[0]),
            "n_max": int(_sim["boot"].shape[1]), "n_warrants": int(len(w)), "n_options_live_bid": int(o["bid_live"].sum()),
            "rows": rows}


def _end_spot_sets(sim, spot, n, vol_stress):
    """({set: horizon spots} fit half, check half) after `n` trading days; stressed sets dropped when vol_stress is 1."""
    sets = scenarios(sim, vol_stress)
    if vol_stress == 1.0:
        sets = {k: v for k, v in sets.items() if k in ("zero", "drift")}
    half = sim["boot"].shape[0] // 2
    ends = {k: spot * np.exp(v[:, n - 1]) for k, v in sets.items()}
    return {k: v[:half] for k, v in ends.items()}, {k: v[half:] for k, v in ends.items()}


def scan_lp_chain(warrant_df, opt_df, contract_size, sim, spot, today, cap, max_loss, objective="credit",
                  vol_stress=VOL_STRESS, min_credit=0.0, r=0.0):
    """One statarb_lp structure per option expiry (pure core of scan_lp)."""
    from logic import static_arb, statarb_lp
    first_day = np.datetime64(sim["asof"]) + np.timedelta64(1, "D")
    n_max = sim["boot"].shape[1]
    rows = []
    for T in sorted(set(int(d) for d in opt_df["days_to_expiry"].unique())):
        expiry = np.datetime64(today) + np.timedelta64(T, "D")
        n = int(np.busday_count(first_day, expiry + np.timedelta64(1, "D")))
        base = {"horizon_dte": T, "trading_days": n}
        if not 1 <= n <= n_max:
            rows.append({**base, "status": "untestable"})
            continue
        longs, shorts, _ = static_arb._build_legs(warrant_df, opt_df, T, contract_size, r)
        fit, check = _end_spot_sets(sim, spot, n, vol_stress)
        row = statarb_lp.solve_horizon(longs, shorts, fit, check, cap, max_loss, objective, min_credit)
        if row is None:
            rows.append({**base, "status": "none", "n_long_legs": len(longs), "n_short_legs": len(shorts)})
            continue
        status = "pure" if row["pure"] else "pass" if row["passes"] else "fail"
        rows.append({**base, **row, "status": status, "n_long_legs": len(longs), "n_short_legs": len(shorts)})
    return rows


def scan_lp(cap=0.05, max_loss=1_000_000, objective="credit", vol_stress=VOL_STRESS, min_credit=1000.0, now=None):
    """Fetch the 2330 chain and run the StatArb MILP at every option expiry against the cached simulation."""
    from logic import options_logic, statarb_lp, warrant_logic
    if not 0 < cap <= 0.5:
        raise ValueError(f"cap must be in (0, 0.5], got {cap}")
    if not 1.0 <= vol_stress <= 3.0:
        raise ValueError(f"vol_stress must be between 1.0 and 3.0, got {vol_stress}")
    if max_loss <= 0:
        raise ValueError("max_loss must be positive")
    if objective not in statarb_lp.OBJECTIVES:
        raise ValueError(f"objective must be one of {statarb_lp.OBJECTIVES}")
    state()
    now = now or datetime.now(TW_TZ)
    w, w_err, w_meta = warrant_logic.read_warrant([STOCK_CODE], "All", 0, 365, 0, 1e9, 0, compute_iv=False)
    o, o_err, _ = options_logic.read_tw_option([STOCK_CODE], "All", min_days=1, compute_iv=False)
    if w.empty or o.empty:
        raise RuntimeError(f"no data: warrants {w_err or len(w)}, options {o_err or len(o)}")
    o = o[o["ask_live"] | o["bid_live"]]
    spot = float(w["underlying_price"].iloc[0])
    contract_size = options_logic._commodity_map()[STOCK_CODE]["exercise_ratio"]
    t0 = datetime.now(TW_TZ)
    rows = scan_lp_chain(w, o, contract_size, _sim, spot, now.date(), cap, max_loss, objective, vol_stress,
                         min_credit, options_logic.R)
    return {"spot": spot, "sim_asof": str(_sim["asof"]), "quotes_as_of": (w_meta or {}).get("as_of"),
            "scanned_at": now.isoformat(timespec="seconds"), "runtime_s": round((datetime.now(TW_TZ) - t0).total_seconds(), 1),
            "cap": cap, "max_loss": max_loss, "objective": objective, "vol_stress": vol_stress, "min_credit": min_credit,
            "n_sims": int(_sim["boot"].shape[0]), "n_warrants": int(len(w)), "n_options": int(len(o)), "rows": rows}
