"""StatArb simulation (logic/statarb_logic.py): chart payload shape and sanity on synthetic prices, and the
intraday-row drop in fetch_prices."""
import sys
import types
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from logic import statarb_logic as S


@pytest.fixture(scope="module")
def payload():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2019-01-02", periods=1700)
    px = pd.Series(500 * np.exp(np.cumsum(rng.standard_t(5, len(idx)) * 0.012)), index=idx)
    return S.compute(px)


def test_payload_has_every_chart(payload):
    for key in ("kpis", "ewma", "zhist", "qq", "volhist", "forecast", "fan", "ttest"):
        assert key in payload
    assert len(payload["forecast"]["sig_path_ann"]) == S.N_MAX
    assert [t["h"] for t in payload["ttest"]] == S.HORIZONS
    assert len(payload["fan"]["boot"]["samples"]) == S.N_SAMPLE_PATHS


def test_vol_path_moves_from_today_toward_long_run(payload):
    f = payload["forecast"]
    first, last = f["sig_path_ann"][0], f["sig_path_ann"][-1]
    assert abs(first - f["today_ann"]) < 1e-6
    assert abs(last - f["lr_ann"]) <= abs(first - f["lr_ann"])


def test_fan_bands_ordered_and_start_near_spot(payload):
    b = payload["fan"]["gbm"]["bands"]
    assert all(b["p1"][i] <= b["p50"][i] <= b["p99"][i] for i in range(S.N_MAX))
    assert abs(b["p50"][0] / payload["s0"] - 1) < 0.01


def test_drift_fan_is_zero_drift_fan_shifted_by_historical_drift(payload):
    mu = payload["kpis"]["drift_ann"] / S.TD_YEAR
    zero, drift = payload["fan"]["boot"]["bands"], payload["fan"]["boot_drift"]["bands"]
    for q in ("p5", "p50", "p95"):
        for i in (0, S.N_MAX - 1):
            assert drift[q][i] == pytest.approx(zero[q][i] * np.exp(mu * (i + 1)), rel=1e-3)


def test_centred_shocks_keep_both_models_zero_mean(payload):
    for t in payload["ttest"]:
        assert abs(t["boot"]["mean"]) < 0.5 and abs(t["gbm"]["mean"]) < 0.5   # percent


def _fake_yf(dates):
    df = pd.DataFrame({("Close", S.TICKER): np.linspace(100, 110, len(dates))}, index=pd.DatetimeIndex(dates))
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return types.SimpleNamespace(download=lambda *a, **k: df)


def test_fetch_drops_todays_row_only_while_session_open(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(["2026-10-07", "2026-10-08"]))
    intraday = datetime(2026, 10, 8, 11, 0, tzinfo=S.TW_TZ)
    after_close = datetime(2026, 10, 8, 15, 0, tzinfo=S.TW_TZ)
    assert S.fetch_prices(intraday).index[-1].date().isoformat() == "2026-10-07"
    assert S.fetch_prices(after_close).index[-1].date().isoformat() == "2026-10-08"


@pytest.fixture(scope="module")
def sim():
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2019-01-02", periods=1700)
    px = pd.Series(500 * np.exp(np.cumsum(rng.standard_t(5, len(idx)) * 0.012)), index=idx)
    return S._build(px)[1]


def _scan(sim, warrants, options):
    w = pd.DataFrame(warrants, columns=["warrant_code", "warrant_name", "type", "strike", "days_to_expiry",
                                        "exercise_ratio", "ask", "ask_qty"])
    cols = ["contract", "type", "strike", "days_to_expiry", "bid", "bid_live"]
    o = pd.DataFrame(options, columns=cols + ["ask"] if options and len(options[0]) == 7 else cols)
    today = (pd.Timestamp(sim["asof"]) + pd.Timedelta(days=1)).date()
    return {(r["warrant_code"], r["option_contract"]): r for r in S.scan_pairs(w, o, 2000, sim, 100.0, today)}


def test_scan_flags_pure_arb_and_scores_loss_region(sim):
    rows = _scan(sim,
                 [("W100", "w", "Call", 100.0, 40, 1.0, 1.0, 5), ("W110", "w", "Call", 110.0, 40, 1.0, 1.0, 5)],
                 [("C110", "Call", 110.0, 30, 3.0, True), ("C100", "Call", 100.0, 30, 6.0, True)])
    pure = rows[("W100", "C110")]                 # long the lower strike, short the higher: never loses
    assert pure["pure"] and pure["credit"] == 4000 and pure["max_loss"] == 0 and pure["p_touch"] is None
    assert pure["price_diff"] == 2.0 and pure["warrant_bid"] is None and pure["exercise_ratio"] == 1.0
    risky = rows[("W110", "C100")]                # short the lower strike: loses above 100 + 5 credit per share
    assert not risky["pure"] and risky["max_loss"] == -10000
    assert risky["loss_region"][0][0] == pytest.approx(105, abs=0.1) and risky["loss_region"][0][1] is None
    assert risky["testable"]
    for k in ("zero", "drift", "stress", "stress_drift"):
        assert 0 < risky["p_expiry"][k] <= risky["p_touch"][k] <= 1
    assert risky["p_touch"]["stress"] >= risky["p_touch"]["zero"]


def test_scan_skips_debits_type_mismatch_and_short_leg_outliving_warrant(sim):
    rows = _scan(sim, [("W100", "w", "Call", 100.0, 20, 1.0, 1.0, 5)],
                 [("C110x", "Call", 110.0, 30, 3.0, True),     # option expires after the warrant
                  ("P110", "Put", 110.0, 10, 3.0, True),       # opposite type
                  ("C110d", "Call", 110.0, 10, 0.4, True),     # net debit
                  ("C110n", "Call", 110.0, 10, 3.0, False)])   # no live bid
    assert rows == {}


def test_scan_skips_crossed_option_quotes(sim):
    rows = _scan(sim, [("W100", "w", "Call", 100.0, 40, 1.0, 1.0, 5)],
                 [("C110", "Call", 110.0, 30, 3.0, True, 2.0),      # bid above ask: stale
                  ("C111", "Call", 111.0, 30, 3.0, True, 3.2)])
    assert set(rows) == {("W100", "C111")}
