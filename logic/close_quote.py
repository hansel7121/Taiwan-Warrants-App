"""Live market inputs for booking a trade's realized P&L at close.

Which leg "survives" a close (the long-dated one), and whether its value came
from a real quote or from the model, is a pricing decision, not request/response
glue — so it lives here rather than inline in the route.

The failure boundary is deliberate, because "no live quote" and "a bug in this
code" used to be indistinguishable (two bare `except Exception: pass`):

- yfinance-backed calls (`adr_premium_scenario`, `us_option_last`) reach the
  public internet and raise on transient upstream faults. That is an *expected*
  outcome for a quote lookup, so it is caught — but logged at WARN, never
  silently swallowed — and the leg falls back to source="model".
- `warrant_logic.get_cmoney_prices` already absorbs its own per-code network
  errors internally (it logs them and returns whatever it got, `{}` in the worst
  case). Anything escaping it is therefore genuinely unexpected, so it is NOT
  caught here and propagates to the caller.
- Only the outbound call sits inside each `try`. Our own parsing of the result
  runs outside it, so a typo'd key raises instead of masquerading as "the market
  had no quote".
"""
from services import applog

from logic import us_options_logic
from logic import warrant_logic


def resolve_survivor(mode, survivor, *, warrant_code=None, us_code=None,
                     opt_type=None, opt_strike=None, opt_expiry_iso=None):
    """Live inputs for the surviving leg of a `mode` trade.

    Returns the ADR premium / FX basis (for the option leg of US / TW-US trades)
    plus a real market quote for the surviving leg where one can be fetched: a
    warrant's current bid from CMoney, or a US ADR option's last price from
    yfinance. `source` is "market" only when such a quote was actually obtained;
    otherwise it stays "model" and the frontend values that leg with
    Black-Scholes. The near (expired) leg always settles at intrinsic, so it
    needs no quote here.
    """
    out = {
        "source": "model",
        "current_premium": None, "current_fx": None, "adr_ratio": None,
        "warrant_bid": None, "opt_last_usd": None,
    }

    # Live ADR premium + FX drive the option leg for US / TW-US trades.
    if mode in ("us", "twus") and us_code:
        scenario = None
        try:
            scenario = us_options_logic.adr_premium_scenario(us_code, 30)
        except Exception as e:
            # Expected failure mode: the premium series is fetched from
            # yfinance, which is down/rate-limited often enough that it must not
            # fail the close. Logged so it is visible instead of vanishing.
            applog.log("CLOSE", f"adr premium unavailable for {us_code}: {e}",
                       level="WARN")
        if scenario is not None:
            out["current_premium"] = scenario.get("current_premium")
            out["current_fx"] = (scenario.get("fx") or {}).get("current_fx")
            out["adr_ratio"] = (us_options_logic._adr_map().get(us_code) or {}).get("adr_ratio")

    if survivor == "warrant" and mode in ("direct", "us"):
        # A real warrant (not a TW option): CMoney has a live bid. No try here —
        # get_cmoney_prices handles its own fetch errors, so an exception out of
        # it is a bug and should surface, not degrade to a silent "model".
        res = warrant_logic.get_cmoney_prices([warrant_code]) if warrant_code else {}
        w = (res.get(warrant_code) or {}).get("Warrant") if res else None
        if w:
            bid = float(w.get("BuyPr1") or 0)
            if bid > 0:
                out["warrant_bid"] = bid
                out["source"] = "market"

    elif survivor == "option" and mode in ("us", "twus") and us_code:
        strike = float(opt_strike or 0)   # outside the try: a malformed strike is a caller bug
        last = None
        try:
            last = us_options_logic.us_option_last(
                us_code, opt_type, strike,
                opt_expiry_iso, out["current_fx"], out["adr_ratio"],
            )
        except Exception as e:
            # Same as above: the option chain comes from yfinance. Missing data
            # returns None on its own; only an upstream fault raises, and that
            # degrades to the model price rather than failing the close.
            applog.log("CLOSE", f"us option quote unavailable for {us_code}: {e}",
                       level="WARN")
        if last:
            out["opt_last_usd"] = last
            out["source"] = "market"

    return out
