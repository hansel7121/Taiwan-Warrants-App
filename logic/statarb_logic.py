"""TSMC underlying simulation for the Arb Finder → StatArb sub-tab (port of tsmc_2330_underlying_sim.ipynb).

`compute(px_all)` turns a dividend-adjusted close series into every chart the tab draws: EWMA vol, the standardised
shock pool, GARCH vol forecast, and 10,000 simulated paths from a filtered block bootstrap and from GBM fed the same
vol path, plus a two-sample t-test between them. `state()` serves the cached result; `update()` refetches and rebuilds.
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

_cache = None
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
        "fan": {"days": (k + 1).tolist(), "boot": _fan(boot, s0), "gbm": _fan(gbm, s0)},
        "ttest": ttest,
    }


def update():
    """Refetch prices and rebuild the cache; one rebuild at a time."""
    global _cache
    with _lock:
        _cache = compute(fetch_prices())
        return _cache


def state():
    """Cached result, built on first use."""
    return _cache if _cache is not None else update()
