"""Live market inputs for booking a trade's realized P&L at close.

Decides which leg "survives" a close and whether its value comes from a real
quote or the model — a pricing decision, so it lives here rather than inline in
the route. yfinance calls are wrapped in `try` (expected to fail sometimes,
logged at WARN, falls back to source="model"); `warrant_logic.get_cmoney_prices`
already absorbs its own errors, so anything escaping it is a genuine bug and
propagates uncaught.
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
            # yfinance is down/rate-limited often enough that it must not fail the close.
            applog.log("CLOSE", f"adr premium unavailable for {us_code}: {e}",
                       level="WARN")
        if scenario is not None:
            out["current_premium"] = scenario.get("current_premium")
            out["current_fx"] = (scenario.get("fx") or {}).get("current_fx")
            out["adr_ratio"] = (us_options_logic._adr_map().get(us_code) or {}).get("adr_ratio")

    if survivor == "warrant" and mode in ("direct", "us"):
        # No try here — get_cmoney_prices handles its own fetch errors.
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
            # Same as above: yfinance fault degrades to model price, not a failed close.
            applog.log("CLOSE", f"us option quote unavailable for {us_code}: {e}",
                       level="WARN")
        if last:
            out["opt_last_usd"] = last
            out["source"] = "market"

    return out
