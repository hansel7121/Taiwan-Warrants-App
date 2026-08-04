"""Offline tick-replay validation harness (ADR 0008).

The primary gate before any live broker connection: replay fixture `Tick`s
through the whole worker detection path — `tick_to_warrant_row` -> `check_tick`
-> the `ARB_STRATEGIES` registry — against a fixture option-side mirror, and
assert on the signal rows that come out.

Scope is deliberately narrow. The strategy math already has tests
(`test_arb_dispatch_registry.py`, `test_arb_logic_no_matches.py`); what is new
and untested is the *wiring*: a broker print becoming a warrant row, and that
row being priced against a mirror instead of a freshly fetched chain. So the
fixtures below reuse the dispatch-registry fixture's known-good trigger values
rather than inventing new ones — anything that changes in the numbers here is a
wiring change, not a math change.

Pure in-memory: no broker client, no scheduler, no network, no Supabase.
"""
from datetime import datetime

import pandas as pd

from logic import arb_logic
from services.broker.base import Tick
from services.broker.tick_translate import tick_to_warrant_row

CODE = "2330"
W_LO = "031101"      # strike 550 wing — the warrant every tick below prints on
W_HI = "031102"      # strike 650 wing — resting book, needed by butterfly
CONTRACT_SIZE = 2000
SPOT = 600.0
TS = datetime(2026, 8, 3, 10, 30)

PARAMS = arb_logic.ScanParams(max_strike_diff_pct=5.0, max_dte_diff=5)


# ── fixtures: the warrant snapshot book, the option mirror, the ticks ─────────

def _snapshot_book():
    """Last known `warrant_logic` rows, keyed by code — what the worker holds
    between periodic refreshes and merges each incoming print into."""
    common = dict(
        underlying_code=CODE, type="Call", underlying_price=SPOT,
        days_to_expiry=60, exercise_ratio=0.5, ask_qty=100, bid_qty=100,
        volume=1000, iv_ask=0.30, iv_bid=0.29, delta_calc=0.6,
        leverage_calc=8.0, time_value=1.0, time_value_pct=0.2, time_value_am=0.5,
    )
    return {
        W_LO: dict(warrant_code=W_LO, warrant_name="W-550", strike=550.0,
                   ask=5.0, bid=4.9, **common),
        W_HI: dict(warrant_code=W_HI, warrant_name="W-650", strike=650.0,
                   ask=1.0, bid=0.9, **common),
    }


def _options():
    common = dict(
        stock_code=CODE, underlying_price=SPOT, days_to_expiry=30,
        ask_live=True, bid_live=True, iv_bid=0.35, volume=100,
    )
    return pd.DataFrame([
        dict(contract="TXO-C600", type="Call", strike=600.0, bid=12.0, ask=12.5, **common),
        dict(contract="TXO-C650", type="Call", strike=650.0, bid=11.0, ask=11.5, **common),
        dict(contract="TXO-P560", type="Put", strike=560.0, bid=8.0, ask=8.5, **common),
    ])


def _mirror():
    return arb_logic.OptionMirror(_options(), {CODE: CONTRACT_SIZE})


def _tick(code=W_LO, price=5.0, ts=TS, broker="kgi"):
    return Tick(code=code, price=price, ts=ts, broker=broker)


# ── the replay drivers ───────────────────────────────────────────────────────

def replay(tick, strategies=None, params=PARAMS):
    """One tick through the exact path the live worker will take."""
    row = tick_to_warrant_row(tick, _snapshot_book()[tick.code])
    return arb_logic.check_tick(row, _mirror(), params=params, strategies=strategies)


def replay_with_resting_book(tick, resting_codes, strategy, params=PARAMS):
    """Same translation, but the printing warrant is matched alongside resting
    snapshot rows instead of alone.

    `check_tick` frames one tick as a one-row warrant DataFrame, so a strategy
    needing several warrant legs (butterfly wants two wings) can never fire from
    it — see `test_butterfly_cannot_fire_from_a_lone_tick`. Replaying the
    multi-leg case therefore has to hand the registry the printing leg plus the
    book, which is what the worker will have to do to support butterfly live.
    Tagging mirrors `check_tick` so the rows come out the same shape.
    """
    rows = [tick_to_warrant_row(tick, _snapshot_book()[tick.code])]
    rows += [_snapshot_book()[c] for c in resting_codes]

    entry = arb_logic.resolve_strategy(strategy)
    out = entry.check(pd.DataFrame(rows), _options(), CONTRACT_SIZE, params)
    for row in out:
        row["strategy"] = entry.name
        if row.get("underlying_code") is None:
            row["underlying_code"] = CODE
    return out


def _by_contract(signals, contract):
    return next(s for s in signals if s["option_contract"] == contract)


# ── true positive: same_type ─────────────────────────────────────────────────

def test_cheap_print_fires_a_same_type_signal():
    """5.0 / 0.5 = 10.0 per underlying share against a 12.0 option bid on the
    favorable (Ko >= Kw) side — the dispatch-registry fixture's trigger, reached
    here from an actual Tick."""
    signals = replay(_tick(price=5.0), strategies=["same_type"])

    row = _by_contract(signals, "TXO-C600")
    assert row["strategy"] == "same_type"
    assert row["underlying_code"] == CODE
    assert row["warrant_code"] == W_LO
    assert row["type"] == "Call"
    assert row["trade"] == "Buy Warrant / Sell Option"
    assert row["warrant_strike"] == 550.0
    assert row["opt_strike"] == 600.0
    assert row["warrant_dte"] == 60
    assert row["opt_dte"] == 30
    assert row["warrant_ask"] == 5.0          # the print, not the snapshot's
    assert row["warrant_per_share"] == 10.0
    assert row["opt_per_share"] == 12.0
    assert row["price_diff"] == 2.0
    assert row["riskless"] is True
    assert row["max_loss_per_share"] == 0.0
    assert row["opt_contract_size"] == CONTRACT_SIZE
    assert row["warrants_needed"] == 4000     # 2000 shares / 0.5 per unit
    assert row["board_lots"] == 4.0
    assert row["fillable"] is True


def test_same_type_emits_every_profitable_contract_not_just_the_closest():
    signals = replay(_tick(price=5.0), strategies=["same_type"])
    assert {s["option_contract"] for s in signals} == {"TXO-C600", "TXO-C650"}
    assert _by_contract(signals, "TXO-C650")["price_diff"] == 1.0


def test_a_print_never_produces_a_short_warrant_signal():
    """The translation zeroes `bid` (a trade print says nothing about the book),
    and the sell-the-warrant direction demands a real bid — so the long-only
    constraint holds for free on every replayed tick."""
    signals = replay(_tick(price=5.0))
    assert all(s.get("trade") != "Buy Option / Sell Warrant" for s in signals)


# ── true positive: pcp ───────────────────────────────────────────────────────

def test_cheap_print_fires_a_pcp_signal_against_the_opposite_type():
    """Synthetic call = P_bid + S - PV(Ko) ~= 8 + 600 - 559.1 ~= 48.9 per share,
    versus 10.0 for the printed warrant."""
    signals = replay(_tick(price=5.0), strategies=["pcp"])

    assert len(signals) == 1
    row = signals[0]
    assert row["strategy"] == "pcp"
    assert row["underlying_code"] == CODE
    assert row["warrant_code"] == W_LO
    assert row["option_contract"] == "TXO-P560"
    assert row["type"] == "Call"
    assert row["opt_type"] == "Put"
    assert row["executable"] is True          # long warrant only
    assert row["warrant_strike"] == 550.0
    assert row["opt_strike"] == 560.0
    assert row["warrant_per_share"] == 10.0
    assert row["opt_per_share"] == 8.0        # sold at the option bid
    assert row["bond_pv"] < 560.0
    assert row["synthetic_price"] > row["warrant_per_share"]
    assert row["price_diff"] == round(row["synthetic_price"] - 10.0, 4)
    assert row["price_diff"] > 0


def test_pcp_ignores_options_outside_the_strike_cap():
    """Only the 560 put is within max_strike_diff_pct of the 550 warrant; the
    call side is the wrong type for PCP entirely."""
    contracts = {s["option_contract"] for s in replay(_tick(price=5.0), strategies=["pcp"])}
    assert contracts == {"TXO-P560"}


# ── true positive: butterfly ─────────────────────────────────────────────────

def test_butterfly_cannot_fire_from_a_lone_tick():
    """Documents the wiring gap: `check_tick` frames one warrant, and a fly
    needs two wings, so butterfly is structurally silent on a bare tick."""
    assert replay(_tick(price=5.0), strategies=["butterfly"]) == []


def test_cheap_print_fires_a_butterfly_against_the_resting_wing():
    """Wings 10.0 + 2.0 per share, body sold at 12.0 x2 -> 12.0 credit; the
    tail 2*600 - 550 - 650 is flat at 0, so the whole credit is locked."""
    signals = replay_with_resting_book(_tick(price=5.0), [W_HI], "butterfly")

    assert len(signals) == 1
    row = signals[0]
    assert row["strategy"] == "butterfly"
    assert row["underlying_code"] == CODE
    assert row["type"] == "Call"
    assert row["trade"] == "Long Wings / Short 2x Body"
    assert row["wing_lo_code"] == W_LO        # the printing leg
    assert row["wing_lo_strike"] == 550.0
    assert row["wing_lo_ask"] == 5.0          # the print, not the snapshot's
    assert row["wing_lo_ps"] == 10.0
    assert row["wing_lo_bid"] is None         # translation cleared it
    assert row["wing_hi_code"] == W_HI        # resting snapshot leg
    assert row["wing_hi_strike"] == 650.0
    assert row["wing_hi_ps"] == 2.0
    assert row["mid_contract"] == "TXO-C600"
    assert row["mid_strike"] == 600.0
    assert row["mid_contracts"] == 2
    assert row["mid_ps"] == 12.0
    assert row["mid_dte"] <= min(row["wing_lo_dte"], row["wing_hi_dte"])
    assert row["credit_ps"] == 12.0
    assert row["tail_ps"] == 0.0
    assert row["guaranteed_ps"] == 12.0
    assert row["guaranteed_profit"] == 12.0 * CONTRACT_SIZE
    assert row["riskless"] is True
    assert row["loose_prices"] is False


def test_a_dear_print_kills_the_butterfly_credit():
    """Same resting wing, same body — only the printed wing moved."""
    assert replay_with_resting_book(_tick(price=15.0), [W_HI], "butterfly") == []


# ── true negative ────────────────────────────────────────────────────────────

def test_a_fairly_priced_print_produces_no_signal_from_any_strategy():
    """The 650 warrant at 8.0 is 16.0 per share against an 11.0 bid on the only
    favorable call, its nearest put is 13.8% away (outside the PCP strike cap),
    and a lone tick cannot make a fly — nothing should fire anywhere."""
    assert replay(_tick(code=W_HI, price=8.0)) == []


def test_the_true_negative_is_not_vacuous():
    """Same warrant, same mirror — only the price changes, so the empty result
    above is the pricing, not a broken fixture."""
    assert replay(_tick(code=W_HI, price=1.0))


# ── replaying a sequence ─────────────────────────────────────────────────────

def test_replaying_a_price_sequence_fires_only_on_the_ticks_that_qualify():
    """The signal follows the live print, not the cached snapshot: the same
    warrant, mirror and params yield signals only below the option bid."""
    prices = [30.0, 24.0, 12.0, 5.0]
    fired = [bool(replay(_tick(price=p), strategies=["same_type"])) for p in prices]
    assert fired == [False, False, False, True]


def test_provenance_survives_the_translation_into_the_signal_path():
    """Ticks from either broker replay identically; the row is priced off the
    print regardless of who sent it."""
    kgi = replay(_tick(price=5.0, broker="kgi"), strategies=["same_type"])
    fubon = replay(_tick(price=5.0, broker="fubon"), strategies=["same_type"])
    assert kgi == fubon


def test_replay_needs_no_mirror_data_for_an_unmirrored_underlying():
    empty = arb_logic.OptionMirror(pd.DataFrame(), {CODE: CONTRACT_SIZE})
    row = tick_to_warrant_row(_tick(price=5.0), _snapshot_book()[W_LO])
    assert arb_logic.check_tick(row, empty, params=PARAMS) == []
