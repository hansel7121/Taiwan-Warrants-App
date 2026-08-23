"""`sync_suggestions` writes rows keyed by a deterministic id — pin that id.

The id is `arb_suggestions`' primary key, so changing how it is built does not
raise anything: it silently re-inserts every already-logged arb under a new key,
duplicating the Suggestions tab. The job runs four times an hour during TW market
hours and had no test at all, so it is pinned here against the recorded Direct
Match fixture rather than a hand-written frame.
"""
import pandas as pd
import pytest

from logic import arb_logic, static_arb
from services import db_suggestions, scheduler
from tests.logic import arb_golden


@pytest.fixture
def captured(monkeypatch):
    """Run the job against fixture rows with both Supabase calls intercepted."""
    frames, params, expected = arb_golden.load("direct_strict")
    rows = expected
    df = pd.DataFrame(rows).sort_values(["riskless", "price_diff_pct"],
                                        ascending=[False, False])

    written = []
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])
    monkeypatch.setattr(arb_logic, "match_warrant_tw_option", lambda *a, **k: df)
    monkeypatch.setattr(static_arb, "match_static_arb", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set())
    monkeypatch.setattr(db_suggestions, "insert_suggestions", written.append)
    scheduler.sync_suggestions()
    return df, written


def test_ids_are_arbtype_direction_warrant_contract(captured):
    df, written = captured
    assert written, "the job wrote nothing"
    for row in written[0]:
        arb_type, direction, warrant_code, contract = row["id"].split(":", 3)
        assert arb_type == "direct_same_type"
        assert direction in ("bw", "bo")
        assert row["legs"]["warrant_code"] == warrant_code
        assert row["legs"]["option_contract"] == contract
        assert row["arb_type"] == arb_type


def test_direction_comes_from_the_trade_label(captured):
    df, written = captured
    for row in written[0]:
        expected = "bw" if row["legs"]["trade"].startswith("Buy Warrant") else "bo"
        assert row["id"].split(":")[1] == expected


def test_ids_are_unique_and_cover_every_row(captured):
    df, written = captured
    ids = [r["id"] for r in written[0]]
    assert len(ids) == len(set(ids))
    assert len(ids) == len(df), "a row was dropped or collapsed onto another id"


def test_already_logged_ids_are_not_reinserted(monkeypatch):
    frames, params, expected = arb_golden.load("direct_strict")
    df = pd.DataFrame(expected)
    written = []
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])
    monkeypatch.setattr(arb_logic, "match_warrant_tw_option", lambda *a, **k: df)
    monkeypatch.setattr(static_arb, "match_static_arb", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set(ids))
    monkeypatch.setattr(db_suggestions, "insert_suggestions", written.append)
    scheduler.sync_suggestions()
    assert written == [], "already-logged suggestions were re-inserted"


LP_ROW = {
    "underlying_code": "2330", "underlying_price": 600.0, "horizon_dte": 30,
    "n_legs": 2, "n_long": 1, "n_short": 1,
    "legs": [
        {"side": "long", "kind": "warrant", "code": "031100", "name": "W", "type": "Call",
         "strike": 580.0, "dte": 30, "eff_strike": 580.0, "quote": 12.0, "price_ps": 24.0,
         "lots": 50, "lot_label": "張", "shares": 25000, "depth_lots": 200,
         "ratio": 0.5, "cash": -600000},
        {"side": "short", "kind": "option", "code": "C600 30d", "name": "C600 30d",
         "type": "Call", "strike": 600.0, "dte": 30, "eff_strike": 600.0, "quote": 30.0,
         "price_ps": 30.0, "lots": 12, "lot_label": "口", "shares": 24000,
         "depth_lots": 200, "ratio": None, "cash": 720000},
    ],
    "net_credit": 120000.0, "min_payoff": 0.0, "guaranteed_profit": 120000.0,
    "worst_spot": 580.0, "gross_debit": 600000.0, "return_pct": 20.0,
    "riskless": True, "fillable": True,
}


@pytest.fixture
def lp_written(monkeypatch):
    """Run the job with Direct empty and one LP row, both Supabase calls stubbed."""
    written = []
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])
    monkeypatch.setattr(arb_logic, "match_warrant_tw_option",
                        lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(static_arb, "match_static_arb",
                        lambda *a, **k: pd.DataFrame([LP_ROW]))
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set())
    monkeypatch.setattr(db_suggestions, "insert_suggestions", written.append)
    scheduler.sync_suggestions()
    return written


def test_lp_rows_are_logged_under_their_own_arb_type(lp_written):
    """The two scanners share the table and are told apart by arb_type — that is
    what the Suggestions tab filters on to show one scanner per sub-tab."""
    assert len(lp_written) == 1 and len(lp_written[0]) == 1
    row = lp_written[0][0]
    assert row["arb_type"] == "static_lp"
    assert row["id"].startswith("static_lp:2330:30:")
    assert row["price_diff"] == LP_ROW["guaranteed_profit"]
    assert row["price_diff_pct"] == LP_ROW["return_pct"]
    assert row["legs"]["legs"] == LP_ROW["legs"]


def test_lp_id_ignores_lot_sizes(monkeypatch):
    """The LP re-solves against whatever depth is resting, so the same structure
    found later carries different lot counts. Keying on the leg set means
    re-finding it logs nothing rather than a near-duplicate."""
    import copy

    a = scheduler._lp_suggestion_id(LP_ROW)
    resized = copy.deepcopy(LP_ROW)
    for leg in resized["legs"]:
        leg["lots"] *= 3
        leg["shares"] *= 3
    assert scheduler._lp_suggestion_id(resized) == a

    relegged = copy.deepcopy(LP_ROW)
    relegged["legs"][1]["code"] = "C650 30d"
    assert scheduler._lp_suggestion_id(relegged) != a


def test_short_warrant_rows_are_flagged(monkeypatch):
    """A short warrant leg is what Direct Match reaches as Buy Option / Sell
    Warrant; flagging it lets the two tabs be lined up by direction."""
    import copy

    row = copy.deepcopy(LP_ROW)
    row["legs"][0]["side"] = "short"
    written = []
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])
    monkeypatch.setattr(arb_logic, "match_warrant_tw_option", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(static_arb, "match_static_arb", lambda *a, **k: pd.DataFrame([row]))
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set())
    monkeypatch.setattr(db_suggestions, "insert_suggestions", written.append)
    scheduler.sync_suggestions()
    assert written[0][0]["legs"]["needs_short_warrant"] is True
    assert lp_no_short(LP_ROW) is False


def lp_no_short(row):
    return any(l.get("side") == "short" and l.get("kind") == "warrant"
               for l in row["legs"])


def test_both_scanners_run_on_one_snapshot(monkeypatch):
    """Both must read the same TTL-cached frames — a refresh landing between two
    separate jobs would make a difference in their output timing, not method."""
    calls = []
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])
    monkeypatch.setattr(arb_logic, "match_warrant_tw_option",
                        lambda *a, **k: calls.append("direct") or pd.DataFrame())
    monkeypatch.setattr(static_arb, "match_static_arb",
                        lambda *a, **k: calls.append("lp") or pd.DataFrame())
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set())
    monkeypatch.setattr(db_suggestions, "insert_suggestions", lambda rows: None)
    scheduler.sync_suggestions()
    assert calls == ["direct", "lp"]


def test_lp_scan_allows_short_warrants(monkeypatch):
    """Only with shorts allowed does the LP's reachable set cover both of Direct
    Match's directions, which is what makes the comparison meaningful."""
    seen = {}
    monkeypatch.setattr(static_arb, "match_static_arb",
                        lambda codes, **k: seen.update(k) or pd.DataFrame())
    scheduler._scan_lp_suggestions(["2330"])
    assert seen["allow_short_warrants"] is True
    assert seen["min_edge"] == 0.0


def test_no_matches_is_not_an_error(monkeypatch):
    """NoMatchesError is a normal empty scan; a genuine RuntimeError must escape."""
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])

    def _raise(exc):
        def _fn(*a, **k):
            raise exc
        return _fn

    monkeypatch.setattr(static_arb, "match_static_arb", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set())
    monkeypatch.setattr(db_suggestions, "insert_suggestions", lambda rows: None)
    monkeypatch.setattr(arb_logic, "match_warrant_tw_option",
                        _raise(arb_logic.NoMatchesError("nothing")))
    scheduler.sync_suggestions()  # must not raise

    monkeypatch.setattr(arb_logic, "match_warrant_tw_option",
                        _raise(RuntimeError("CMoney down")))
    with pytest.raises(RuntimeError):
        scheduler.sync_suggestions()
