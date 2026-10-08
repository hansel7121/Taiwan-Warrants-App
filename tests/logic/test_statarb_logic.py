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
