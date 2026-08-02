"""resolve_survivor's market-vs-model split and its failure boundary.

The point of the extraction was that "no live quote" and "a bug" used to look
identical (two bare `except Exception: pass`). These tests pin the boundary
that replaced them:

- a yfinance-backed lookup that raises (adr_premium_scenario / us_option_last)
  is an expected upstream fault: logged, then falls back to source="model";
- an exception out of warrant_logic.get_cmoney_prices — which already absorbs
  its own network errors — is a bug and must propagate.

Only the true I/O boundaries are patched; close_quote itself runs for real.
"""
import pytest

from logic import close_quote
from logic import us_options_logic
from logic import warrant_logic

US = "2330"
WCODE = "031234"


def _quote(bid):
    """A CMoney payload shaped like get_cmoney_prices' output for one code."""
    return {WCODE: {"Warrant": {"BuyPr1": bid}, "Stock": {}}}


def _boom(*a, **k):
    raise RuntimeError("upstream exploded")


@pytest.fixture
def stub_adr(monkeypatch):
    """A working ADR premium/FX/ratio basis, so US paths get past it."""
    monkeypatch.setattr(us_options_logic, "adr_premium_scenario",
                        lambda code, horizon: {"current_premium": 0.05,
                                               "fx": {"current_fx": 32.0}})
    monkeypatch.setattr(us_options_logic, "_adr_map",
                        lambda: {US: {"adr_ratio": 5.0}})


# ── (a)/(b) warrant survivor ────────────────────────────────────────────────

def test_warrant_survivor_with_a_live_bid_is_sourced_from_market(monkeypatch):
    monkeypatch.setattr(warrant_logic, "get_cmoney_prices", lambda codes: _quote(2.35))

    out = close_quote.resolve_survivor("direct", "warrant", warrant_code=WCODE)

    assert out["warrant_bid"] == pytest.approx(2.35)
    assert out["source"] == "market"


def test_warrant_survivor_without_a_quote_falls_back_to_model(monkeypatch):
    monkeypatch.setattr(warrant_logic, "get_cmoney_prices", lambda codes: {})

    out = close_quote.resolve_survivor("direct", "warrant", warrant_code=WCODE)

    assert out["warrant_bid"] is None
    assert out["source"] == "model"


def test_warrant_survivor_with_a_zero_bid_is_not_a_market_quote(monkeypatch):
    # CMoney returns the row but with no resting bid — a real, expected state.
    monkeypatch.setattr(warrant_logic, "get_cmoney_prices", lambda codes: _quote(0))

    out = close_quote.resolve_survivor("direct", "warrant", warrant_code=WCODE)

    assert out["warrant_bid"] is None
    assert out["source"] == "model"


# ── (c)/(d) US option survivor ──────────────────────────────────────────────

def test_option_survivor_with_a_live_last_is_sourced_from_market(stub_adr, monkeypatch):
    seen = {}

    def fake_last(us_code, opt_type, strike, expiry, fx, ratio):
        seen.update(us_code=us_code, strike=strike, fx=fx, ratio=ratio)
        return 1.8

    monkeypatch.setattr(us_options_logic, "us_option_last", fake_last)

    out = close_quote.resolve_survivor("us", "option", us_code=US, opt_type="Call",
                                       opt_strike=900, opt_expiry_iso="2026-09-18")

    assert out["opt_last_usd"] == pytest.approx(1.8)
    assert out["source"] == "market"
    # The live premium basis is both returned and fed into the option lookup.
    assert out["current_premium"] == pytest.approx(0.05)
    assert out["current_fx"] == pytest.approx(32.0)
    assert out["adr_ratio"] == pytest.approx(5.0)
    assert seen == {"us_code": US, "strike": 900.0, "fx": 32.0, "ratio": 5.0}


def test_option_survivor_with_no_match_falls_back_to_model(stub_adr, monkeypatch):
    # us_option_last returns None for every expected missing-data case.
    monkeypatch.setattr(us_options_logic, "us_option_last", lambda *a, **k: None)

    out = close_quote.resolve_survivor("us", "option", us_code=US, opt_type="Put",
                                       opt_strike=900, opt_expiry_iso="2026-09-18")

    assert out["opt_last_usd"] is None
    assert out["source"] == "model"
    assert out["current_fx"] == pytest.approx(32.0)   # basis still returned


# ── (e) the failure boundary ────────────────────────────────────────────────

def test_yfinance_option_fault_is_logged_and_degrades_to_model(stub_adr, monkeypatch, capsys):
    monkeypatch.setattr(us_options_logic, "us_option_last", _boom)

    out = close_quote.resolve_survivor("us", "option", us_code=US, opt_type="Call",
                                       opt_strike=900, opt_expiry_iso="2026-09-18")

    assert out["opt_last_usd"] is None
    assert out["source"] == "model"
    # Not silent, unlike the bare `except Exception: pass` this replaced.
    logged = capsys.readouterr().out
    assert "upstream exploded" in logged
    assert "WARN" in logged


def test_adr_premium_fault_is_logged_and_leaves_the_basis_empty(monkeypatch, capsys):
    monkeypatch.setattr(us_options_logic, "adr_premium_scenario", _boom)
    monkeypatch.setattr(us_options_logic, "_adr_map", lambda: {US: {"adr_ratio": 5.0}})
    monkeypatch.setattr(us_options_logic, "us_option_last", lambda *a, **k: None)

    out = close_quote.resolve_survivor("us", "option", us_code=US, opt_type="Call",
                                       opt_strike=900, opt_expiry_iso="2026-09-18")

    assert out["current_premium"] is None
    assert out["current_fx"] is None
    assert out["adr_ratio"] is None
    assert out["source"] == "model"
    logged = capsys.readouterr().out
    assert "upstream exploded" in logged
    assert "WARN" in logged


def test_cmoney_fault_propagates_instead_of_being_swallowed(monkeypatch):
    # get_cmoney_prices handles its own per-code network errors internally, so
    # an exception escaping it is a genuine bug and must NOT look like "no
    # live quote".
    monkeypatch.setattr(warrant_logic, "get_cmoney_prices", _boom)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        close_quote.resolve_survivor("direct", "warrant", warrant_code=WCODE)


# ── shape / no-op paths ─────────────────────────────────────────────────────

def test_unknown_survivor_returns_the_full_model_shape():
    out = close_quote.resolve_survivor("direct", None)

    assert out == {"source": "model", "current_premium": None, "current_fx": None,
                   "adr_ratio": None, "warrant_bid": None, "opt_last_usd": None}
