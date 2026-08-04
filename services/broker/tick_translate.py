"""Turn a broker `Tick` into the warrant row `arb_logic.check_tick` consumes.

A Tick is one trade print — code, price, timestamp, broker — and nothing else
(docs/adr/0003). Every field the arb matchers need beyond the price (type,
strike, exercise ratio, underlying price, days to expiry, depth) is static or
slow-moving warrant metadata that only the periodic `warrant_logic` snapshot
carries, so a translation is a merge: last known snapshot row for that code,
with the price fields replaced by the print.

Pure and dependency-free on purpose — the tick-replay harness (#32) and the
live worker (#33) both call it, and neither should need a broker connection,
Supabase, or a Flask context to translate a tick.
"""
from services.broker.base import Tick


# Fields carried by the snapshot's price at the moment it was taken. They are
# wrong the instant a new print arrives, and are cheaper to drop than to
# re-derive: the matchers recompute IV and time value from `ask` when absent.
_STALE_ON_REPRICE = (
    "iv_ask", "iv_bid", "delta_calc", "leverage_calc",
    "time_value", "time_value_pct", "time_value_am",
)


class TickTranslationError(ValueError):
    """The tick and the snapshot row cannot honestly be merged."""


def tick_to_warrant_row(tick, snapshot_row):
    """Merge one `Tick` into the last known warrant row for the same code.

    `snapshot_row` is any mapping in `warrant_logic.COL_ORDER` shape — a dict or
    a DataFrame row (`df.iloc[i]`). Returns a new dict; neither input is
    mutated. Raises `TickTranslationError` when the codes disagree or the print
    is not a usable price, rather than emitting a row that would price an arb
    off the wrong instrument.
    """
    if not isinstance(tick, Tick):
        raise TickTranslationError(f"not a Tick: {type(tick).__name__}")
    if snapshot_row is None:
        raise TickTranslationError(f"no snapshot row for {tick.code}")

    row = dict(snapshot_row)
    if not row:
        raise TickTranslationError(f"empty snapshot row for {tick.code}")

    snapshot_code = row.get("warrant_code")
    if snapshot_code is None or str(snapshot_code) != str(tick.code):
        raise TickTranslationError(
            f"tick code {tick.code!r} does not match snapshot row {snapshot_code!r}")

    price = float(tick.price)
    if not price > 0:
        raise TickTranslationError(f"non-positive tick price for {tick.code}: {tick.price!r}")

    # Every warrant leg is BOUGHT (warrants are long-only), and every matcher
    # prices that leg off `ask`, so the print lands there. `bid` is zeroed
    # rather than left at its snapshot value: a trade print says nothing about
    # the book, and a stale bid against a fresh ask is a fabricated spread. The
    # matchers already treat a non-positive bid as "unknown" and fall back to
    # `ask` for the exit leg.
    row["ask"] = price
    row["bid"] = 0.0
    for field in _STALE_ON_REPRICE:
        row.pop(field, None)

    # `days_to_expiry` is passed through unchanged: CMoney gives only LastDays
    # (a whole-day count), never an expiry date, so there is nothing to
    # recompute it from — and the snapshot behind a tick is at most one intraday
    # refresh old, which cannot cross a day boundary.

    row["tick_ts"] = tick.ts
    row["tick_broker"] = tick.broker
    return row
