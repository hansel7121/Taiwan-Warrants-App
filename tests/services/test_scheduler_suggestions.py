"""`sync_suggestions` writes rows keyed by a deterministic id — pin that id.

The id is `arb_suggestions`' primary key, so changing how it is built does not
raise anything: it silently re-inserts every already-logged arb under a new key,
duplicating the Suggestions tab. The job runs four times an hour during TW market
hours and had no test at all, so it is pinned here against the recorded Direct
Match fixture rather than a hand-written frame.
"""
import pandas as pd
import pytest

from logic import arb_logic
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
    monkeypatch.setattr(db_suggestions, "existing_ids", lambda ids: set(ids))
    monkeypatch.setattr(db_suggestions, "insert_suggestions", written.append)
    scheduler.sync_suggestions()
    assert written == [], "already-logged suggestions were re-inserted"


def test_no_matches_is_not_an_error(monkeypatch):
    """NoMatchesError is a normal empty scan; a genuine RuntimeError must escape."""
    monkeypatch.setattr(scheduler, "warrant_universe", lambda: ["2330"])
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330"])

    def _raise(exc):
        def _fn(*a, **k):
            raise exc
        return _fn

    monkeypatch.setattr(arb_logic, "match_warrant_tw_option",
                        _raise(arb_logic.NoMatchesError("nothing")))
    scheduler.sync_suggestions()  # must not raise

    monkeypatch.setattr(arb_logic, "match_warrant_tw_option",
                        _raise(RuntimeError("CMoney down")))
    with pytest.raises(RuntimeError):
        scheduler.sync_suggestions()
