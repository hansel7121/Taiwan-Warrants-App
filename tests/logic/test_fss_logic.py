"""Forced Short Squeeze engine (logic/fss_logic.py): parsers, calendar, expanding quintiles, the entry rules and
trade marks on a small synthetic panel."""
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from logic import fss_logic as F


def test_roc_date_spellings():
    assert F.roc_date("115.10.08") == date(2026, 10, 8)
    assert F.roc_date("115年10月08日") == date(2026, 10, 8)
    assert F.roc_date("115/07/01") == date(2026, 7, 1)
    assert F.roc_date("1151013") == date(2026, 10, 13)
    assert F.roc_date("2026-01-02") == date(2026, 1, 2)
    assert F.roc_date("") is None and F.roc_date("--") is None


def test_tw_datetime_missing_time_is_end_of_day():
    assert F.tw_datetime(date(2026, 6, 15), "14:28:42").hour == 14
    assert F.tw_datetime(date(2026, 6, 15), "101225").minute == 12
    assert F.tw_datetime(date(2026, 6, 15), None).hour == 23


def test_parsers():
    mi = {"tables": [
        {"title": "報酬指數(臺灣證券交易所)", "data": [["發行量加權股價報酬指數", "115,170.92"]]},
        {"title": "115年10月07日 每日收盤行情(全部)", "data": [
            ["1101", "台泥", "24,862,063", "8,553", "632,455,261", "25.25", "25.70", "25.10", "25.50"],
            ["00400A", "ETF", "1", "1", "1", "1", "1", "1", "1"],
            ["2330", "台積電", "0", "0", "0", "--", "--", "--", "--"]]}]}
    prices, tr = F.parse_mi_index(mi)
    assert tr == 115170.92
    assert prices["1101"] == ("台泥", 25.5, 24862063.0, 632455261.0)
    assert "00400A" not in prices and np.isnan(prices["2330"][1])
    mg = {"tables": [{"title": "融資融券彙總 (全部)", "data": [
        ["1101", "台泥", "1", "2", "3", "4", "5", "6", "7", "8", "9", "1,000", "1,234", "13", "14", " "]]}]}
    assert F.parse_mi_margn(mg) == {"1101": 1234.0}
    bfi = {"data": [["0056", "元大高股息", "115.10.16", "115.10.21", "分配收益"], ["2603", "長榮", "115.10.20", "115.10.26", "除息"]]}
    assert F.parse_bfi84u(bfi) == [("2603", "長榮", date(2026, 10, 20), "除息")]
    hol = {"data": [["2026-02-11", "農曆春節前最後交易日", ""], ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
                    ["2026-02-23", "農曆春節後開始交易日", ""], ["2026-10-09", "國慶日", ""]]}
    assert F.parse_holidays(hol) == {date(2026, 2, 12), date(2026, 10, 9)}
    agm = [{"公司代號": "1101", "公司名稱": "台泥", "股東常(臨時)會日期-常或臨時": "常會", "停止過戶起訖日期-起": "1150424",
            "公告日期": "1150310", "公告時間": "153000"}]
    (sid, _, reason, start, ann), = F.parse_t187ap38(agm)
    assert (sid, reason, start, ann.hour) == ("1101", F.AGM_REASON, date(2026, 4, 24), 15)


def test_parse_t108sb27_takes_the_earlier_ex_date_and_posting_time():
    cells = ["1101", "台泥", "114年", "115/07/07", "", "", "115/06/30", "0.8", "", "", "115/07/01", "115/07/28",
             "", "", "", "7,493,181,742", "115/06/15", "14:28:42", "新台幣10.0000元"]
    html = "<table><tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr></table>"
    (sid, _, ex, ann), = F.parse_t108sb27(html)
    assert (sid, ex) == ("1101", date(2026, 6, 30))
    assert ann == datetime(2026, 6, 15, 14, 28, 42, tzinfo=F.TW_TZ)


def test_calendar_skips_weekends_and_holidays():
    past = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    cal = F.Calendar(past, {date(2026, 10, 9)}, horizon=3)
    assert cal.days[4:] == [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    assert cal.index(date(2026, 10, 10)) == 4          # a Saturday maps to the next trading day
    assert cal.at(cal.index(date(2026, 10, 13)) - 4) == date(2026, 10, 6)   # 12, 8, 7, 6 (9th is a holiday)


def test_bucket_edges_are_inclusive_at_q5():
    e = [0.01, 0.03, 0.07, 0.15]
    assert [F.bucket(x, e) for x in (0.0, 0.01, 0.05, 0.149, 0.15, 3.0)] == [1, 2, 3, 4, 5, 5]
    assert F.bucket(None, e) is None and F.bucket(0.2, None) is None


def test_population_uses_only_deadlines_before_the_signal_day_and_burns_in():
    d0 = date(2020, 1, 1)
    days = [d0 + timedelta(days=i) for i in range(F.Q_MIN_HIST + 50)]
    pop = F.Population(days, np.arange(len(days), dtype=float))
    assert pop.edges_as_of(days[F.Q_MIN_HIST - 1])[0] is None           # 199 strictly earlier deadlines
    edges, n = pop.edges_as_of(days[F.Q_MIN_HIST])
    assert n == F.Q_MIN_HIST
    assert edges[3] == pytest.approx(np.quantile(np.arange(F.Q_MIN_HIST), 0.8))


# ── a synthetic market: 300 trading days, one hot stock (9999) and one cold (8888) ──

def _market(n=300):
    days = pd.bdate_range("2025-01-01", periods=n)
    rng = np.random.default_rng(0)
    taiex = 1000 * np.cumprod(1 + rng.normal(0, 0.01, n))
    rows, mg = [], []
    for sid, sb, vol in (("9999", 5000.0, 2e6), ("8888", 10.0, 2e6)):
        close = 100 * np.cumprod(1 + rng.normal(0, 0.015, n))
        for d, c in zip(days, close):
            rows.append((d, sid, sid, c, vol, vol * c))
            mg.append((d, sid, sb))
    return F.Panel(pd.DataFrame(rows, columns=["date", "stock_id", "name", "close", "volume", "value"]),
                   pd.DataFrame(mg, columns=["date", "stock_id", "short_bal"]),
                   pd.DataFrame({"date": days, "taiex_tr": taiex}), pd.DataFrame(columns=["date", "stock_id", "adj"]))


def _pool():
    """250 past deadlines with days-to-cover 0..2.49 -> Q5 cutoff ~2.0; the hot stock's 2.5 is Q5, the cold 0.005 is not."""
    return F.Population([date(2024, 1, 1) + timedelta(days=i) for i in range(250)], np.arange(250) / 100)


def _event(panel, sid, d_i, reasons=("除息",), known_days_before_entry=3):
    entry = panel.dates[d_i - F.K]
    return {"id": f"{sid}:{panel.dates[d_i]}", "stock_id": sid, "name": sid, "d_date": panel.dates[d_i],
            "reasons": list(reasons),
            "known_at": datetime.combine(entry - timedelta(days=known_days_before_entry), datetime.min.time(), F.TW_TZ)}


def test_evaluate_trades_q5_and_skips_the_rest():
    panel = _market()
    cal = F.Calendar(panel.dates, set())
    d_i = 280
    evs = [_event(panel, "9999", d_i), _event(panel, "8888", d_i),
           _event(panel, "9999", d_i - 40, reasons=("減資",)), _event(panel, "9999", d_i - 80, known_days_before_entry=-1)]
    ups, trades = F.evaluate(evs, panel, cal, _pool(), date(2000, 1, 1), panel.dates[-1])
    st = {u["id"]: (u["status"], u["note"]) for u in ups}
    assert st[evs[0]["id"]][0] == "traded"
    assert "bucket Q" in st[evs[1]["id"]][1]
    assert "reason not traded" in st[evs[2]["id"]][1]
    assert "not public before the D-6 close" in st[evs[3]["id"]][1]
    (t,) = trades
    assert t["state"] == "closed" and len(t["marks"]) == F.K + F.H
    assert t["net_ret"] == pytest.approx(t["long_ret"] + t["short_ret"])


def test_known_at_cutoff_is_13_25_on_the_entry_day():
    panel = _market()
    cal = F.Calendar(panel.dates, set())
    ev = _event(panel, "9999", 280)
    entry = panel.dates[280 - F.K]
    for hhmm, status in (((13, 24), "traded"), ((13, 25), "skipped")):
        ev["known_at"] = datetime(entry.year, entry.month, entry.day, *hhmm, tzinfo=F.TW_TZ)
        ups, _ = F.evaluate([ev], panel, cal, _pool(), date(2000, 1, 1), panel.dates[-1])
        assert ups[0]["status"] == status


def test_future_deadlines_watch_then_signal_then_open_long():
    panel = _market()
    cal = F.Calendar(panel.dates, set())
    last = panel.T - 1
    far = _event(panel, "9999", last - 20)
    far["d_date"] = cal.at(last + 30)
    near = {**_event(panel, "9999", last - 20), "id": "9999:near", "d_date": cal.at(last + 3)}
    near["known_at"] = datetime(2024, 1, 1, tzinfo=F.TW_TZ)
    mid = {**near, "id": "9999:mid", "d_date": cal.at(last + 10)}
    ups, trades = F.evaluate([far, mid, near], panel, cal, _pool(), date(2000, 1, 1), panel.dates[-1])
    st = {u["id"]: u["status"] for u in ups}
    assert st[far["id"]] == "watching" and st["9999:mid"] == "signal" and st["9999:near"] == "traded"
    (t,) = trades
    assert t["state"] == "long" and len(t["marks"]) == F.K - 3


def test_go_live_date_blocks_older_entries():
    panel = _market()
    cal = F.Calendar(panel.dates, set())
    ups, trades = F.evaluate([_event(panel, "9999", 280)], panel, cal, _pool(), panel.dates[-1], panel.dates[-1])
    assert not trades and "entry before go-live" in ups[0]["note"]


def test_marks_charge_costs_on_d_and_d_plus_h_and_flip_sign():
    panel = _market()
    beta = 1.2
    m = panel.marks("9999", 280, beta)
    cost = F.COST_LEG_BPS / 1e4 + beta * F.HEDGE_BPS / 1e4
    assert [x["off"] for x in m] == list(range(-F.K + 1, F.H + 1))
    for x in m:
        sign = 1 if x["off"] <= 0 else -1
        expect = sign * (x["stock_ret"] - beta * x["hedge_ret"]) - (cost if x["off"] in (0, F.H) else 0)
        assert x["pnl"] == pytest.approx(expect)


def test_cluster_merges_close_deadlines_and_prefers_the_exchange_date():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
    cal = F.Calendar(days, set())
    t0 = datetime(2026, 1, 1, tzinfo=F.TW_TZ)
    rows = [{"id": "a", "stock_id": "1", "d_date": days[10], "reasons": ["除權息"], "sources": {"mops_t108": "x"}, "known_at": t0},
            {"id": "b", "stock_id": "1", "d_date": days[11], "reasons": ["除息"], "sources": {"bfi84u": "x"},
             "known_at": t0 + timedelta(days=5)},
            {"id": "c", "stock_id": "1", "d_date": days[30], "reasons": ["股東常會"], "sources": {}, "known_at": None}]
    a, c = F.cluster(rows, cal)
    assert a["id"] == "b" and a["members"] == ["a", "b"] and a["known_at"] == t0
    assert c["id"] == "c" and c["known_at"] is None


def test_daily_pnl_sums_trades_by_date():
    t = [{"marks": [{"date": "2026-01-02", "pnl": 0.01}, {"date": "2026-01-03", "pnl": -0.02}]},
         {"marks": [{"date": "2026-01-03", "pnl": 0.03}]}]
    rows = F.daily_pnl(t, 1_000_000)
    assert [(r["date"], round(r["pnl"]), r["n_open"], round(r["cum"])) for r in rows] == \
        [("2026-01-02", 10000, 1, 10000), ("2026-01-03", 10000, 2, 20000)]
