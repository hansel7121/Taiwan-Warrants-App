"""The warrant-leg strategy registry.

`match_warrant_tw_option` and the future live per-tick worker both reach the
same three matchers through `arb_logic.ARB_STRATEGIES` (ADR 0003). These tests
pin the registry's contents, prove each registered entry is a faithful
pass-through to the matcher it wraps (the refactor must not move any math), and
exercise the per-tick entry point the worker tickets will call.

Everything here is pure in-memory pandas — no fetcher is touched.
"""
import pandas as pd
import pytest

from logic import arb_logic

CODE = "2330"
CONTRACT_SIZE = 2000
SPOT = 600.0


def _warrants():
    """Two call warrants on the same underlying (butterfly needs two wings)."""
    common = dict(
        underlying_code=CODE, underlying_price=SPOT, type="Call",
        days_to_expiry=60, exercise_ratio=0.5, ask_qty=100, bid_qty=100,
        iv_ask=0.3,
    )
    return pd.DataFrame([
        dict(warrant_code="0301", warrant_name="W-550", strike=550.0,
             ask=5.0, bid=4.9, **common),
        dict(warrant_code="0302", warrant_name="W-650", strike=650.0,
             ask=1.0, bid=0.9, **common),
    ])


def _options():
    common = dict(
        stock_code=CODE, underlying_price=SPOT, days_to_expiry=30,
        ask_live=True, bid_live=True, iv_bid=0.35, volume=100,
    )
    return pd.DataFrame([
        dict(contract="TXO-C600", type="Call", strike=600.0,
             bid=12.0, ask=12.5, **common),
        dict(contract="TXO-C650", type="Call", strike=650.0,
             bid=11.0, ask=11.5, **common),
        dict(contract="TXO-P560", type="Put", strike=560.0,
             bid=8.0, ask=8.5, **common),
    ])


PARAMS = arb_logic.ScanParams(max_strike_diff_pct=5.0, max_dte_diff=5)


# ── registry contents ────────────────────────────────────────────────────────

def test_registry_holds_exactly_the_three_strategies():
    assert arb_logic.strategy_names() == ["same_type", "pcp", "butterfly"]


def test_every_entry_is_named_and_callable():
    for entry in arb_logic.ARB_STRATEGIES:
        assert entry.name
        assert callable(entry.match)


def test_only_cross_type_strategies_need_the_full_chain():
    """PCP pairs against the opposite type and butterfly needs the whole ladder,
    so both must override the caller's warrant-side type filter."""
    need = {e.name: e.needs_full_chain for e in arb_logic.ARB_STRATEGIES}
    assert need == {"same_type": False, "pcp": True, "butterfly": True}


def test_resolve_strategy_returns_the_registered_entry():
    for name in arb_logic.strategy_names():
        assert arb_logic.resolve_strategy(name).name == name


def test_unknown_strategy_falls_back_to_the_default():
    """The strategy string arrives from the client, so an unknown one degrades
    to same_type exactly as the old if/elif `else` branch did."""
    assert arb_logic.resolve_strategy("nope").name == arb_logic.DEFAULT_STRATEGY
    assert arb_logic.resolve_strategy(None).name == "same_type"


# ── dispatch equals calling the matcher directly ─────────────────────────────

@pytest.mark.parametrize("loose", [False, True])
def test_same_type_dispatch_matches_direct_call(loose):
    p = arb_logic.ScanParams(PARAMS.max_strike_diff_pct, PARAMS.max_dte_diff, loose)
    direct = arb_logic._match_warrants_to_options(
        _warrants(), _options(), CONTRACT_SIZE,
        p.max_strike_diff_pct, p.max_dte_diff, positive_loose=loose,
    )
    via = arb_logic.resolve_strategy("same_type").check(
        _warrants(), _options(), CONTRACT_SIZE, p)
    assert via == direct


@pytest.mark.parametrize("loose", [False, True])
def test_pcp_dispatch_matches_direct_call(loose):
    p = arb_logic.ScanParams(PARAMS.max_strike_diff_pct, PARAMS.max_dte_diff, loose)
    direct = arb_logic._match_warrants_pcp(
        _warrants(), _options(), CONTRACT_SIZE,
        p.max_strike_diff_pct, p.max_dte_diff, positive_loose=loose,
    )
    via = arb_logic.resolve_strategy("pcp").check(
        _warrants(), _options(), CONTRACT_SIZE, p)
    assert via == direct


@pytest.mark.parametrize("loose", [False, True])
def test_butterfly_dispatch_matches_direct_call(loose):
    p = arb_logic.ScanParams(PARAMS.max_strike_diff_pct, PARAMS.max_dte_diff, loose)
    direct = arb_logic._match_butterflies(
        _warrants(), _options(), CONTRACT_SIZE, positive_loose=loose)
    via = arb_logic.resolve_strategy("butterfly").check(
        _warrants(), _options(), CONTRACT_SIZE, p)
    assert via == direct


def test_butterfly_ignores_the_proximity_caps():
    """Butterfly is anchored by its wings, not by a target strike, so the
    shared ScanParams fields must not leak into it."""
    wide = arb_logic.ScanParams(max_strike_diff_pct=0.0, max_dte_diff=0)
    entry = arb_logic.resolve_strategy("butterfly")
    assert (entry.check(_warrants(), _options(), CONTRACT_SIZE, wide)
            == entry.check(_warrants(), _options(), CONTRACT_SIZE, PARAMS))


def test_fixture_actually_produces_rows():
    """Guard against the equality tests above passing on two empty lists."""
    for name in ("same_type", "pcp", "butterfly"):
        rows = arb_logic.resolve_strategy(name).check(
            _warrants(), _options(), CONTRACT_SIZE, PARAMS)
        assert rows, f"{name} fixture matched nothing"


# ── the per-tick entry point ─────────────────────────────────────────────────

def _mirror():
    return arb_logic.OptionMirror(_options(), {CODE: CONTRACT_SIZE})


def _tick(**over):
    tick = _warrants().iloc[0].to_dict()
    tick.update(over)
    return tick


def test_option_mirror_serves_only_the_matching_underlying():
    mirror = _mirror()
    assert len(mirror.options_for(CODE)) == 3
    assert mirror.options_for("2303").empty
    assert mirror.contract_size(CODE) == CONTRACT_SIZE


def test_check_tick_runs_every_strategy_and_tags_the_rows():
    signals = arb_logic.check_tick(_tick(), _mirror())

    assert signals
    assert {s["strategy"] for s in signals} <= set(arb_logic.strategy_names())
    assert all(s["underlying_code"] == CODE for s in signals)


def test_check_tick_is_callable_per_strategy():
    """Each registered strategy can be driven on its own — the worker and the
    replay harness both dispatch one check at a time."""
    for name in arb_logic.strategy_names():
        signals = arb_logic.check_tick(_tick(), _mirror(), strategies=[name])
        assert all(s["strategy"] == name for s in signals)


def test_check_tick_agrees_with_a_one_row_scan():
    """A tick is just a one-warrant frame: same math, same rows."""
    tick = _tick()
    one_row = pd.DataFrame([tick])
    for name in arb_logic.strategy_names():
        expected = arb_logic.resolve_strategy(name).check(
            one_row, _options(), CONTRACT_SIZE, PARAMS)
        got = arb_logic.check_tick(tick, _mirror(), params=PARAMS, strategies=[name])
        for row, want in zip(got, expected):
            # check_tick adds only the strategy tag and the underlying
            # backfill the full scan also applies; the math must be untouched.
            assert row.pop("strategy") == name
            row.pop("underlying_code", None)
            want.pop("underlying_code", None)
        assert got == expected


def test_check_tick_reprices_when_the_tick_moves():
    """The live quote on the tick — not a cached frame — drives the edge."""
    cheap = arb_logic.check_tick(_tick(ask=5.0), _mirror(), strategies=["same_type"])
    dear = arb_logic.check_tick(_tick(ask=50.0), _mirror(), strategies=["same_type"])

    assert cheap
    assert not dear  # warrant now dearer per share than the option bid


def test_check_tick_returns_nothing_when_the_mirror_has_no_chain():
    empty = arb_logic.OptionMirror(pd.DataFrame(), {CODE: CONTRACT_SIZE})
    assert arb_logic.check_tick(_tick(), empty) == []
    assert arb_logic.check_tick(_tick(underlying_code="9999"), _mirror()) == []


def test_check_tick_returns_nothing_without_a_contract_size():
    mirror = arb_logic.OptionMirror(_options(), {CODE: None})
    assert arb_logic.check_tick(_tick(), mirror) == []


def test_check_tick_does_not_mutate_the_tick():
    tick = _tick()
    before = dict(tick)
    arb_logic.check_tick(tick, _mirror())
    assert tick == before
