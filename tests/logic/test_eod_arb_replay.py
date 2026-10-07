"""End-of-day arb replay (logic/eod_arb_replay.py) against synthetic tick logs
from scripts/gen_fake_ticks.py: a no-arb day must log nothing, and every arb
injected into the arb day must be logged as an episode at the right times."""
import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from logic import arb_kernels_py, eod_arb_replay as eod, live_arb_logic

TW = ZoneInfo("Asia/Taipei")
_SPEC = importlib.util.spec_from_file_location(
    "gen_fake_ticks", Path(__file__).resolve().parents[2] / "scripts" / "gen_fake_ticks.py")
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

DAY = gen.TRADE_DATE


def _t(hms):
    return datetime.combine(DAY, datetime.strptime(hms, "%H:%M:%S").time(), TW)


@pytest.fixture(scope="module")
def days(tmp_path_factory):
    d = tmp_path_factory.mktemp("ticks")
    out = {}
    for name, arb in (("noarb", False), ("arb", True)):
        path = d / f"{name}.csv"
        gen.generate(path, arb, every_s=6.0)
        out[name] = eod.replay(eod.iter_ticks(path), DAY)
    return out


def test_no_arb_day_logs_nothing(days):
    r = days["noarb"]
    assert r["stats"]["n_ticks"] > 1000
    assert r["direct"] == []
    assert r["lp"] == []


def test_direct_episodes_match_injections(days):
    eps = [(e["open_row"]["warrant_code"], e["open_row"]["option_code"], e["started_at"], e["ended_at"],
            e["open_at_close"]) for e in days["arb"]["direct"]]
    a_w, a_o = gen.DIRECT_A
    b_w, b_o = gen.DIRECT_B
    # Same pair twice = two separate episodes, with exact start/end.
    assert (a_w, a_o, _t("10:15:03"), _t("10:15:45"), False) in eps
    assert (a_w, a_o, _t("11:02:10"), _t("11:02:30"), False) in eps
    # Still open at the bell: closed at 13:30 and flagged.
    assert (b_w, b_o, _t("13:25:00"), _t("13:30:00"), True) in eps
    # Nothing outside the injected windows, and only the injected warrants.
    for w, _, start, end, _ in eps:
        assert w in (a_w, b_w)
        assert start in (_t("10:15:03"), _t("11:02:10"), _t("13:25:00"))
    durations = {e["started_at"]: e["duration_s"] for e in days["arb"]["direct"] if e["key"] == f"{a_w}:{a_o}"}
    assert durations == {_t("10:15:03"): 42.0, _t("11:02:10"): 20.0}


def test_lp_catches_every_window_including_option_only_arb(days):
    eps = days["arb"]["lp"]
    windows = [(_t("10:15:03"), _t("10:15:45")), (_t("11:02:10"), _t("11:02:30")),
               (_t("12:00:00"), _t("12:02:00")), (_t("13:25:00"), _t("13:30:00"))]
    for start, end in windows:
        inside = [e for e in eps if start <= e["started_at"] < end]
        assert inside, f"no LP episode in {start:%H:%M:%S}-{end:%H:%M:%S}"
        assert min(e["started_at"] for e in inside) == start
    for e in eps:
        assert any(s <= e["started_at"] and e["ended_at"] <= en for s, en in windows)
    # The 12:00 vertical is option-only: Direct Match cannot see it, the LP must.
    noon = [e for e in eps if e["started_at"] == _t("12:00:00")]
    assert any(gen.LP_CHEAP_CALL in {l["code"] for l in e["open_row"]["legs"]} for e in noon)
    assert not any(e["started_at"] == _t("12:00:00") for e in days["arb"]["direct"])


def test_records_are_flat_and_complete(days):
    d = eod.direct_records(days["arb"]["direct"], DAY)
    lp = eod.lp_records(days["arb"]["lp"], DAY)
    assert len({r["id"] for r in d}) == len(d) and len({r["id"] for r in lp}) == len(lp)
    r = next(x for x in d if x["started_at"].startswith("2026-10-07T10:15:03"))
    assert r["duration_s"] == 42.0 and r["warrant_ask"] and r["opt_bid"] and r["price_diff"] > 0
    assert all(x["legs"] and x["guaranteed_profit"] > 0 and x["leg_codes"] for x in lp)


def test_incremental_direct_equals_full_rescan(tmp_path):
    """Pairwise-incremental Direct rescans must equal rescanning everything after every change."""
    path = tmp_path / "slice.csv"
    gen.generate(path, True, every_s=3.0, start=datetime.strptime("10:14", "%H:%M").time(),
                 end=datetime.strptime("10:17", "%H:%M").time())
    got = eod.replay(eod.iter_ticks(path), DAY, run_lp=False)["direct"]

    books = {"warrant": {}, "option": {}}
    tracker = eod.EpisodeTracker("price_diff")
    stats = {"n_ticks": 0, "n_ws": 0}
    last = None
    for ts, group in eod._groups_by_ts(eod.iter_ticks(path), stats):
        for t in group:
            books[t["kind"]][t["code"]] = eod._book_row(t)
        last = ts
        hits = live_arb_logic.scan(list(books["warrant"].values()), list(books["option"].values()), DAY,
                                   pairs_fn=arb_kernels_py.direct_pairs)
        tracker.observe(ts, {eod.direct_key(h): h for h in hits})
    want = tracker.finish(last)
    assert [(e["key"], e["started_at"], e["ended_at"]) for e in got] == \
           [(e["key"], e["started_at"], e["ended_at"]) for e in want]
    assert got  # the 10:15:03 injection is inside this slice


def test_episode_tracker_opens_peaks_and_closes():
    tr = eod.EpisodeTracker("price_diff")
    t0 = datetime(2026, 10, 7, 9, 0, tzinfo=TW)
    tr.observe(t0, {"a": {"price_diff": 1.0}})
    tr.observe(t0 + timedelta(seconds=5), {"a": {"price_diff": 3.0}, "b": {"price_diff": 1.0}})
    tr.observe(t0 + timedelta(seconds=9), {"b": {"price_diff": 0.5}})
    tr.observe(t0 + timedelta(seconds=12), {"a": {"price_diff": 2.0}, "b": {"price_diff": 0.5}})
    eps = tr.finish(t0 + timedelta(seconds=20))
    summary = [(e["key"], e["duration_s"], e["peak_row"]["price_diff"], e["open_at_close"]) for e in eps]
    assert summary == [("a", 9.0, 3.0, False), ("b", 15.0, 1.0, True), ("a", 8.0, 2.0, True)]


def test_out_of_session_ticks_fold_but_do_not_open_episodes(tmp_path):
    path = tmp_path / "late.csv"
    gen.generate(path, True, every_s=5.0, start=datetime.strptime("13:24", "%H:%M").time(),
                 end=datetime.strptime("13:40", "%H:%M").time())
    eps = eod.replay(eod.iter_ticks(path), DAY, run_lp=False)["direct"]
    assert eps and all(e["ended_at"] <= _t("13:30:00") for e in eps)


def test_parse_row_handles_blanks():
    row = eod.parse_row({"ts": "2026-10-07T09:00:00.000+08:00", "kind": "warrant", "code": "030015",
                         "name": "x", "type": "Put", "strike": "", "exercise_ratio": "", "expiry": "",
                         "dte": "", "bid": "1.5", "ask": "", "bid_size": "3", "ask_size": "", "src": "snapshot"})
    assert row["strike"] is None and row["expiry"] is None and row["bid"] == 1.5 and row["ask"] is None
    assert eod.parse_row({"ts": "bad", "kind": "warrant", "code": "1"}) is None
    assert DAY == date(2026, 10, 7)


def test_lp_screen_gives_the_same_episodes_as_always_solving(tmp_path):
    """The warm-started screen only skips solves that would have returned None."""
    path = tmp_path / "noon.csv"
    gen.generate(path, True, every_s=2.0, start=datetime.strptime("11:58", "%H:%M").time(),
                 end=datetime.strptime("12:03", "%H:%M").time())
    fast = eod.replay(eod.iter_ticks(path), DAY)
    slow = eod.replay(eod.iter_ticks(path), DAY, use_screen=False)

    def summary(r):
        return [(e["key"], e["started_at"], e["ended_at"], e["peak_row"]["guaranteed_profit"]) for e in r["lp"]]
    assert summary(fast) and summary(fast) == summary(slow)
    assert fast["stats"]["n_lp_solves"] < slow["stats"]["n_lp_solves"]


def test_holiday_file_with_only_snapshot_rows_logs_nothing(tmp_path):
    """A weekday holiday: the recorder starts and snapshots stale books, but nothing ever ticks."""
    import csv
    src = tmp_path / "arb.csv"
    gen.generate(src, True, every_s=30.0)
    snap = tmp_path / "tsmc_ticks_20261007.csv"
    with open(src, newline="") as fi, open(snap, "w", newline="") as fo:
        r = csv.DictReader(fi)
        w = csv.DictWriter(fo, fieldnames=r.fieldnames)
        w.writeheader()
        w.writerows(row for row in r if row["src"] == "snapshot")
    spec = importlib.util.spec_from_file_location(
        "eod_cli", Path(__file__).resolve().parents[2] / "scripts" / "eod_arb_replay.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    res = cli.run(csv_path=str(snap), dry_run=True)
    assert res["status"] == "no_ticks" and res["direct"] == [] and res["lp"] == []
