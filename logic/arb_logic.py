"""Arbitrage / cross-market matching math.

Pure computation extracted from app.py: warrant<->option matching, put-call
parity pairing, and the TW/US option-leg and ADR-premium arb builders. No Flask
context is touched here — every function takes plain args and returns a
DataFrame or list-of-dicts; the routes in app.py do all request/response work.

Market data comes only through the logic modules' fetchers (warrant_logic,
options_logic, us_options_logic), which own their own in-process caches; this
module keeps no cache of its own.
"""
from services import applog
import numpy as np
import pandas as pd
from logic import options_logic
from logic import us_options_logic
from logic import warrant_logic


# ── Straddle arbitrage (volatility relative-value) ───────────────────────────
# A "straddle package" = one call leg + one put leg on the same underlying (their
# strikes may differ within ΔK — a strangle — so packages carry net delta). We
# LONG the cheapest-implied-vol package (legs from either warrant or option, all
# bought) and SHORT the dearest-implied-vol package (OPTION legs only, since
# Taiwan warrants can't be shorted). Edge = short_iv − long_iv, in vol points.
# The comparison is done in IMPLIED VOL, not price, to strip out the intrinsic
# |F−K| (strike) and √T (expiry) terms that dominate raw straddle prices.
#
# IV is scale-invariant: the sigma that reprices a per-share quote at ratio=1 is
# the same sigma the warrant leg carries at its own exercise ratio, so warrant
# iv_ask/iv_bid (already solved at fetch time) and the option IV solved here at
# ratio=1.0 are directly comparable. options_logic's fetch-time IV instead mixes
# a per-share price with the contract ratio (~2000), mis-scaling it, so it is
# never trusted here — the option leg's IV is always recomputed at ratio=1.0
# below, exactly as _match_warrants_to_options marks its option leg.

def _straddle_legs(warrant_df, opt_df, loose=False, short_warrants=False):
    """Flatten warrants + options into a common leg record list.

    px_* are per-underlying-share (warrant quote ÷ exercise ratio; options are
    already per-share).

    ``loose`` (DEBUG) prices every leg on its *favorable* side — buy at the bid,
    write at the ask — instead of the executable side (buy ask / write bid). It
    does not cross the spread, so any edge it reports is not tradeable; it exists
    to confirm the scanner wires up at all when live quotes are thin. Mirrors the
    ``positive_loose`` toggle in the Direct / US arb finders.

    ``short_warrants`` (DEBUG) marks warrant legs shortable. Taiwan warrants can
    only be written by the issuing bank, so any row using a short warrant leg is
    NOT executable — this exists purely to enlarge the short-side candidate pool
    when the option chain has too few live quotes to form packages.
    """
    legs = []
    for _, w in warrant_df.iterrows():
        ratio = float(w["exercise_ratio"] or 0)
        ask = float(w["ask"] or 0)
        if ratio <= 0 or ask <= 0:
            continue
        bid = float(w["bid"]) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else 0.0

        def _wiv(col):
            v = w.get(col)
            return float(v) if pd.notna(v) and 0 < float(v) <= 3 else None

        iv_ask, iv_bid = _wiv("iv_ask"), _wiv("iv_bid")
        # Executable: buy at ask. Loose: buy at bid (favorable, not tradeable).
        buy_px, buy_iv = (bid, iv_bid) if loose else (ask, iv_ask)
        # Write side is the mirror: executable hits the bid, loose lifts the ask.
        sell_px, sell_iv = (ask, iv_ask) if loose else (bid, iv_bid)
        legs.append({
            "source": "warrant", "type": w["type"], "K": float(w["strike"]),
            "dte": int(w["days_to_expiry"]), "id": str(w["warrant_code"]),
            "name": w["warrant_name"], "ratio": ratio,
            "px_buy": round(buy_px / ratio, 4) if buy_px > 0 else None,
            "iv_buy": buy_iv,
            "px_sell": (round(sell_px / ratio, 4) if (short_warrants and sell_px > 0) else None),
            "iv_sell": sell_iv if short_warrants else None,
            "shortable": bool(short_warrants),
            "S": float(w["underlying_price"]), "vol": int(w.get("volume") or 0),
        })
    for _, o in opt_df.iterrows():
        ask = float(o["ask"] or 0) if pd.notna(o.get("ask")) else 0.0
        bid = float(o["bid"] or 0) if pd.notna(o.get("bid")) else 0.0
        S = float(o["underlying_price"])
        K = float(o["strike"])
        T = int(o["days_to_expiry"]) / 365.0
        is_put = o["type"] == "Put"
        # Compute option IV OURSELVES from the per-share quote (ratio=1.0) so it's
        # on the same scale as the warrant IV (see module note above).
        def _oiv(px):
            if px <= 0:
                return None
            v = warrant_logic.implied_vol(px, S, K, T, options_logic.R, 1.0, is_put)
            return round(float(v), 4) if pd.notna(v) and 0 < float(v) <= 3 else None

        buy_px = bid if loose else ask
        sell_px = ask if loose else bid
        legs.append({
            "source": "option", "type": o["type"], "K": K,
            "dte": int(o["days_to_expiry"]), "id": str(o["contract"]),
            "name": o["contract"], "ratio": float(o.get("exercise_ratio") or 2000),
            "px_buy": round(buy_px, 4) if buy_px > 0 else None, "iv_buy": _oiv(buy_px),
            "px_sell": round(sell_px, 4) if sell_px > 0 else None, "iv_sell": _oiv(sell_px),
            "shortable": True,
            "S": S, "vol": int(o.get("volume") or 0),
        })
    return legs


def _collapse_best(legs, typ, side):
    """Keep the single best leg per (strike, dte) for one side.

    side="buy": legs with px_buy+iv_buy, keep MIN iv_buy (cheapest vol to buy).
    side="sell": shortable legs with px_sell+iv_sell, keep MAX iv_sell (dearest
    vol to write). Collapses many same-strike issuer warrants to the best one.
    """
    best = {}
    for lg in legs:
        if lg["type"] != typ:
            continue
        if side == "buy":
            if lg["px_buy"] is None or lg["iv_buy"] is None:
                continue
            iv = lg["iv_buy"]
        else:
            if not lg["shortable"] or lg["px_sell"] is None or lg["iv_sell"] is None:
                continue
            iv = lg["iv_sell"]
        key = (round(lg["K"], 2), lg["dte"])
        cur = best.get(key)
        if cur is None or (side == "buy" and iv < cur[0]) or (side == "sell" and iv > cur[0]):
            best[key] = (iv, lg)
    return [v[1] for v in best.values()]


def _straddles(calls, puts, side, S, max_k_pct, max_dte_diff):
    """Form call+put packages within ΔK/ΔDTE. Returns list of straddle dicts."""
    out = []
    for c in calls:
        for p in puts:
            if abs(c["K"] - p["K"]) / S * 100 > max_k_pct:
                continue
            if abs(c["dte"] - p["dte"]) > max_dte_diff:
                continue
            if side == "buy":
                iv = (c["iv_buy"] + p["iv_buy"]) / 2
                px = c["px_buy"] + p["px_buy"]
            else:
                iv = (c["iv_sell"] + p["iv_sell"]) / 2
                px = c["px_sell"] + p["px_sell"]
            out.append({
                "call": c, "put": p, "iv": iv, "px": px,
                "K": (c["K"] + p["K"]) / 2, "dte": min(c["dte"], p["dte"]),
            })
    return out


def build_straddle_arb(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                       min_volume=0, min_iv_edge=1.0, loose=False,
                       short_warrants=False, require_dte_cover=True):
    """Straddle vol-arb rows: LONG cheap package vs SHORT dear option package.

    option_type is accepted for API symmetry but ignored — a straddle needs both
    calls and puts, so both are always fetched.

    ``loose`` / ``short_warrants`` are DEBUG relaxations — see _straddle_legs.
    Rows produced under either flag are not executable.

    ``require_dte_cover`` enforces PER OPTION TYPE that the long leg of each type
    outlives the short leg of that type (long_call_dte >= short_call_dte and
    long_put_dte >= short_put_dte, independently). A call is covered only by a
    call and a put only by a put: assignment on a short call means delivering
    shares (source them by exercising the long call), assignment on a short put
    means buying shares (offload via the long put). A long call cannot cover a
    short put — the put is assigned precisely in the down-scenario where the call
    is worthless. Without cover the scanner emits rows that leave a naked short,
    the same failure the Direct arb guards against with ``opt_dte <= warrant_dte``.
    """
    all_rows = []
    # Only a genuine data-fetch failure is an error. "This code has no warrants
    # / no live options / no package survived the filters" is a normal empty
    # scan and must not be raised as one (see match_warrant_tw_option).
    skip_reasons = []
    hard_errors = []
    # Stage counters so a zero-row scan can explain WHICH filter emptied it
    # rather than failing with a bare "no matches".
    diag = {"legs": 0, "longs": 0, "shorts": 0, "pairs": 0,
            "cut_dte_cover": 0, "cut_edge": 0, "best_edge": None}
    CONTRACT = 2000  # one TAIFEX single-stock option = 2000 shares

    for code in stock_codes:
        if code not in options_logic._commodity_map():
            skip_reasons.append(f"{code}: no options data available")
            continue

        # Warrant leg needs its solved iv_ask/iv_bid, so compute_iv=True; the
        # option leg's IV is recomputed here at ratio=1.0, so its fetch skips it.
        warrant_df, werr, wmeta = warrant_logic.read_warrant(
            [code], "All", 0, 365, 0, 1e9, 0, compute_iv=True
        )
        if warrant_df.empty:
            msg = f"{code}: {werr or 'no warrants'}"
            if (wmeta or {}).get("hard_error"):
                hard_errors.append(msg)
            else:
                skip_reasons.append(msg)
            continue
        try:
            opt_df, oerr, ometa = options_logic.read_tw_option([code], "All", min_days=1, compute_iv=False)
            if opt_df.empty:
                msg = f"{code}: {oerr or 'no options'}"
                if (ometa or {}).get("hard_error"):
                    hard_errors.append(msg)
                else:
                    skip_reasons.append(msg)
                continue
            opt_df = opt_df[opt_df["ask_live"] | opt_df["bid_live"]]
            if min_volume > 0:
                opt_df = opt_df[opt_df["volume"] >= min_volume]
        except Exception as e:
            hard_errors.append(f"{code}: {e}")
            applog.log("ARB", f"{code} straddle leg fetch failed: {e}", level="ERROR")
            continue
        if opt_df.empty:
            skip_reasons.append(f"{code}: no live options")
            continue

        legs = _straddle_legs(warrant_df, opt_df, loose=loose,
                              short_warrants=short_warrants)
        if not legs:
            continue
        diag["legs"] += len(legs)
        S = float(warrant_df["underlying_price"].iloc[0])

        buy_calls = _collapse_best(legs, "Call", "buy")
        buy_puts = _collapse_best(legs, "Put", "buy")
        sell_calls = _collapse_best(legs, "Call", "sell")
        sell_puts = _collapse_best(legs, "Put", "sell")
        if not (buy_calls and buy_puts and sell_calls and sell_puts):
            continue

        longs = _straddles(buy_calls, buy_puts, "buy", S, max_strike_diff_pct, max_dte_diff)
        shorts = _straddles(sell_calls, sell_puts, "sell", S, max_strike_diff_pct, max_dte_diff)
        diag["longs"] += len(longs)
        diag["shorts"] += len(shorts)
        if not (longs and shorts):
            continue

        seen = set()
        for sh in shorts:
            # Best long counterpart = lowest long_iv within K/DTE tolerance of
            # this short package (maximises the vol edge).
            short_ids = {sh["call"]["id"], sh["put"]["id"]}
            best_long = None
            for lo in longs:
                if abs(lo["K"] - sh["K"]) / S * 100 > max_strike_diff_pct:
                    continue
                if abs(lo["dte"] - sh["dte"]) > max_dte_diff:
                    continue
                # Never buy and write the SAME contract — that just crosses its
                # own bid-ask spread and shows a phantom edge.
                if {lo["call"]["id"], lo["put"]["id"]} & short_ids:
                    continue
                # Cover is PER OPTION TYPE: the long leg of each type must outlive
                # the short leg of that type (see docstring).
                if require_dte_cover and (
                    sh["call"]["dte"] > lo["call"]["dte"]
                    or sh["put"]["dte"] > lo["put"]["dte"]
                ):
                    diag["cut_dte_cover"] += 1
                    continue
                if best_long is None or lo["iv"] < best_long["iv"]:
                    best_long = lo
            if best_long is None:
                continue
            diag["pairs"] += 1
            edge_pts = (sh["iv"] - best_long["iv"]) * 100
            if diag["best_edge"] is None or edge_pts > diag["best_edge"]:
                diag["best_edge"] = edge_pts
            if edge_pts < min_iv_edge:
                diag["cut_edge"] += 1
                continue

            lc, lp = best_long["call"], best_long["put"]
            sc, sp = sh["call"], sh["put"]
            key = (lc["id"], lp["id"], sc["id"], sp["id"])
            if key in seen:
                continue
            seen.add(key)

            def _leg(lg, side):
                px = lg["px_buy"] if side == "buy" else lg["px_sell"]
                iv = lg["iv_buy"] if side == "buy" else lg["iv_sell"]
                lots = round(CONTRACT / lg["ratio"] / 1000, 3) if lg["source"] == "warrant" else 1
                return {
                    "leg": ("Long " if side == "buy" else "Short ") + lg["type"],
                    "source": lg["source"], "type": lg["type"], "K": round(lg["K"], 2),
                    "dte": lg["dte"], "id": lg["id"], "name": lg["name"],
                    "px_per_share": px, "iv": round(iv, 4), "ratio": lg["ratio"],
                    "board_lots": lots,  # 張 for a warrant leg; 1 = one option contract
                }

            net_cash = round((sh["px"] - best_long["px"]) * CONTRACT, 0)
            all_rows.append({
                "underlying_code": code,
                "underlying_price": round(S, 2),
                "long_call": _leg(lc, "buy"), "long_put": _leg(lp, "buy"),
                "short_call": _leg(sc, "sell"), "short_put": _leg(sp, "sell"),
                "long_iv": round(best_long["iv"], 4), "short_iv": round(sh["iv"], 4),
                "iv_edge_pts": round(edge_pts, 2),
                "long_cost_ps": round(best_long["px"], 4), "short_credit_ps": round(sh["px"], 4),
                "net_cash": net_cash,   # NT$ per 2000-share package; + = net credit
                "eff_k_long": round(best_long["K"], 2), "eff_k_short": round(sh["K"], 2),
                "k_diff_pct": round(abs(best_long["K"] - sh["K"]) / S * 100, 2),
                "t_long": best_long["dte"], "t_short": sh["dte"],
                "first_expiry": min(lc["dte"], lp["dte"], sc["dte"], sp["dte"]),
                "contract_size": CONTRACT,
                # Executability flags — any True means this row is debug-only.
                "loose_prices": bool(loose),
                "shorts_warrant": bool(sc["source"] == "warrant" or sp["source"] == "warrant"),
            })

    if not all_rows:
        why = (f"No straddle rows. Stages: legs={diag['legs']}, "
               f"long_pkgs={diag['longs']}, short_pkgs={diag['shorts']}, "
               f"pairs={diag['pairs']}, cut_by_dte_cover={diag['cut_dte_cover']}, "
               f"cut_by_min_iv_edge={diag['cut_edge']}")
        if diag["best_edge"] is not None:
            why += (f". Best edge found = {diag['best_edge']:.2f} pts vs "
                    f"threshold {min_iv_edge:.2f} pts — lower Min IV Edge to see it")
        elif diag["shorts"] == 0:
            why += (". No short packages: the short leg needs a live two-sided "
                    "option quote (or enable Debug: allow short warrants)")
        if skip_reasons:
            why += " | " + "; ".join(skip_reasons)
        if hard_errors:
            # A leg's data source actually failed — that IS an error.
            raise RuntimeError(why + " | " + "; ".join(hard_errors))
        # Every leg was read fine; nothing survived the filters. Most scans end
        # here (a vol edge is rare), so this is a normal empty result, not a
        # failure — keep the stage diagnosis in the log and return empty.
        applog.log("ARB", why)
        return pd.DataFrame()

    result = pd.DataFrame(all_rows).sort_values("iv_edge_pts", ascending=False)
    return result.head(200)


def _match_warrants_to_options(warrant_df, opt_df, opt_contract_size,
                               max_strike_diff_pct, max_dte_diff,
                               positive_loose=False):
    rows = []
    seen = set()  # deduplicate (warrant_code, option_contract) pairs

    for direction in ("positive", "negative"):
        for _, w in warrant_df.iterrows():
            candidates = opt_df[opt_df["type"] == w["type"]].copy()
            if candidates.empty:
                continue

            candidates["strike_diff_pct"] = (
                (candidates["strike"] - w["strike"]).abs() / w["strike"] * 100
            )
            candidates["dte_diff"] = (
                (candidates["days_to_expiry"] - w["days_to_expiry"]).abs()
            )

            # The residual after pairing two same-type contracts is a vertical
            # spread; it must be the FAVORABLE one (payoff >= 0 at every spot) so
            # the entry credit is never clawed back at expiry.
            #   calls: favorable iff K_short >= K_long (bull call spread)
            #   puts : favorable iff K_short <= K_long (bear put spread)
            # Puts invert because a put pays off downward — the short leg must be
            # the one that starts paying LAST. Positive = buy warrant / sell
            # option (long leg = warrant); negative flips the comparison.
            is_put_w = w["type"] == "Put"
            long_side_is_warrant = direction == "positive"
            opt_ge_warrant = long_side_is_warrant != is_put_w
            favorable = (
                candidates["strike"] >= w["strike"]
                if opt_ge_warrant
                else candidates["strike"] <= w["strike"]
            )
            candidates["favorable"] = favorable
            # Max loss per share of the residual vertical at expiry: zero on the
            # favorable side (payoff >= 0 everywhere), |Kw - Ko| on the other.
            candidates["max_loss_per_share"] = np.where(
                favorable, 0.0, (candidates["strike"] - w["strike"]).abs()
            )
            # max_strike_diff_pct bounds that MAX LOSS, not similarity. On the
            # favorable side the loss is already zero however far the strike sits,
            # so the cap must not apply there — a deep-OTM option whose per-share
            # price still beats the warrant is a riskless arb. Only the
            # unfavorable side, where the spread can be clawed back at expiry,
            # needs the cap. (The far favorable side is self-policing: as Ko runs
            # from Kw the option cheapens per share and the price test rejects it.)
            strike_ok = favorable | (candidates["strike_diff_pct"] <= max_strike_diff_pct)

            # Never hold the SHORT leg as the longer-dated one — the long
            # (hedge) leg would expire first, leaving a naked short position.
            #   Positive: short = option -> require opt_dte <= warrant_dte.
            #   Negative: short = warrant -> require warrant_dte <= opt_dte,
            #     with max_dte_diff bounding the remaining (safe) gap.
            # The safe side (long leg outliving the short) is otherwise fine.
            if direction == "positive":
                dte_ok = candidates["days_to_expiry"] <= w["days_to_expiry"]
            else:
                bad_dte = (w["days_to_expiry"] - candidates["days_to_expiry"]).clip(lower=0)
                dte_ok = bad_dte <= max_dte_diff

            candidates = candidates[strike_ok & dte_ok]
            if candidates.empty:
                continue

            ratio = float(w["exercise_ratio"])
            if ratio <= 0:
                continue  # can't size or normalise a warrant with no exercise ratio
            warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
            warrant_bid_live = pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0
            # Loose marks tolerate the ask-as-bid fallback; an EXECUTABLE sell
            # does not — nobody lifts a bid that isn't there. Keep both and let
            # the tight path below demand the real one.
            warrant_bid_val = float(w["bid"]) if warrant_bid_live else float(w["ask"])
            warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
            warrant_bid_real_per_share = round(float(w["bid"]) / ratio, 4) if warrant_bid_live else None
            warrants_needed = round(opt_contract_size / ratio)
            is_put = w["type"] == "Put"
            # IV per leg (matched pair only, cheap) so the modal can mark an OTM
            # surviving leg at its time value ("sell") — the fetch dropped IV.
            if pd.notna(w.get("iv_ask")):
                warrant_iv = round(float(w["iv_ask"]), 4)
            else:
                _wiv = warrant_logic.implied_vol(
                    float(w["ask"]), float(w["underlying_price"]), float(w["strike"]),
                    int(w["days_to_expiry"]) / 365.0, 0.02, ratio, is_put)
                warrant_iv = round(float(_wiv), 4) if pd.notna(_wiv) and 0 < float(_wiv) <= 3 else None
            warrant_bid_disp = round(float(w["bid"]), 4) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else None

            # Emit EVERY profitable option for this warrant, not just the
            # strike/DTE-closest one — a farther-but-profitable pair must not be
            # hidden behind a closer-but-unprofitable "best".
            for _, opt in candidates.iterrows():
                # Each arb direction executes against only ONE side of the option
                # book, so a one-sided quote is still a valid benchmark. The chain
                # now carries per-side liveness flagged BEFORE the settlement/last
                # fallback backfilled the missing side, so read them directly —
                # opt["ask"]/["bid"] are post-fallback and can't tell a genuine
                # quote from a stale settlement mark. Each formula below then
                # requires the side it actually trades.
                opt_ask_live = bool(opt.get("ask_live", False))
                opt_bid_live = bool(opt.get("bid_live", False))
                if not (opt_ask_live or opt_bid_live):
                    continue
                opt_bid_per_share = round(float(opt["bid"]), 4) if opt_bid_live else None
                opt_ask_per_share = round(float(opt["ask"]), 4) if opt_ask_live else None

                # Both directions honour the loose/tight toggle symmetrically.
                # Tight = the prices you can actually hit: LIFT the ask on whatever
                # you buy, HIT the bid on whatever you sell. Loose = the optimistic
                # (favorable-side) mark, for ranking only.
                #   positive (buy warrant / sell option)
                #     tight: opt_bid  - warrant_ask  > 0
                #     loose: opt_ask  - warrant_bid  > 0
                #   negative (buy option / sell warrant)
                #     tight: opt_ask  - warrant_bid  < 0   (real warrant bid only)
                #     loose: opt_bid  - warrant_ask  < 0
                # Negative previously used the loose formula unconditionally, which
                # priced a BUY at the bid and a SELL at the ask — both unattainable.
                if direction == "positive":
                    if positive_loose:
                        if opt_ask_per_share is None:
                            continue  # loose positive marks against the option ask
                        price_diff = round(opt_ask_per_share - warrant_bid_per_share, 4)
                        exec_opt = opt_ask_per_share
                        exec_warrant = warrant_bid_per_share
                    else:
                        if opt_bid_per_share is None:
                            continue  # tight positive sells the option at its bid
                        price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)
                        exec_opt = opt_bid_per_share
                        exec_warrant = warrant_ask_per_share
                    if price_diff <= 0:
                        continue
                else:
                    if positive_loose:
                        if opt_bid_per_share is None:
                            continue  # loose negative marks against the option bid
                        price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)
                        exec_opt = opt_bid_per_share
                        exec_warrant = warrant_ask_per_share
                    else:
                        # Executable: buy the option at its ask, sell the warrant
                        # into its bid. A synthetic (ask-as-bid) warrant bid would
                        # fake the proceeds, so demand the REAL one.
                        if opt_ask_per_share is None or warrant_bid_real_per_share is None:
                            continue
                        price_diff = round(opt_ask_per_share - warrant_bid_real_per_share, 4)
                        exec_opt = opt_ask_per_share
                        exec_warrant = warrant_bid_real_per_share
                    if price_diff >= 0:
                        continue

                pair_key = (w["warrant_code"], opt["contract"])
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                price_diff_pct = round(price_diff / exec_opt * 100, 2) if exec_opt > 0 else None

                if direction == "positive":
                    trade = "Buy Warrant / Sell Option"
                    # Buying the warrant lifts the ask -> resting SELL size gates it.
                    warrant_depth_lots = int(w.get("ask_qty") or 0)
                else:
                    trade = "Buy Option / Sell Warrant"
                    # Selling the warrant hits the bid -> resting BUY size gates it.
                    warrant_depth_lots = int(w.get("bid_qty") or 0)
                board_lots_needed = round(warrants_needed / 1000, 4)
                fillable = warrant_depth_lots >= board_lots_needed

                if pd.notna(opt.get("iv_bid")):
                    opt_iv = round(float(opt["iv_bid"]), 4)
                else:
                    # Mark off the LIVE side(s) only — a settlement-backfilled
                    # side would put a stale price into the IV solve.
                    if opt_bid_per_share is not None and opt_ask_per_share is not None:
                        _omid = (opt_bid_per_share + opt_ask_per_share) / 2
                    else:
                        _omid = opt_ask_per_share if opt_ask_per_share is not None else opt_bid_per_share
                    _oiv = warrant_logic.implied_vol(
                        _omid, float(opt["underlying_price"]), float(opt["strike"]),
                        int(opt["days_to_expiry"]) / 365.0, options_logic.R, 1.0, is_put) if _omid else None
                    opt_iv = round(float(_oiv), 4) if (_oiv is not None and pd.notna(_oiv) and 0 < float(_oiv) <= 3) else None

                rows.append({
                    "warrant_code": w["warrant_code"],
                    "warrant_name": w["warrant_name"],
                    "option_contract": opt["contract"],
                    "type": w["type"],
                    "trade": trade,
                    "underlying_price": w["underlying_price"],
                    "warrant_dte": int(w["days_to_expiry"]),
                    "opt_dte": int(opt["days_to_expiry"]),
                    "dte_diff": int(opt["dte_diff"]),
                    "warrant_strike": w["strike"],
                    "opt_strike": round(float(opt["strike"]), 2),
                    "strike_diff_pct": round(float(opt["strike_diff_pct"]), 2),
                    # Favorable residual = the vertical pays >= 0 at every spot, so
                    # the entry credit is never clawed back: a true (riskless) arb.
                    # Otherwise the credit is at risk up to max_loss_per_share
                    # (per underlying share, before exercise-ratio sizing).
                    "riskless": bool(opt["favorable"]),
                    "max_loss_per_share": round(float(opt["max_loss_per_share"]), 4),
                    "warrants_needed": warrants_needed,
                    "board_lots": board_lots_needed,
                    "warrant_depth_lots": warrant_depth_lots,
                    "fillable": bool(fillable),
                    "opt_contract_size": opt_contract_size,
                    "warrant_ask": w["ask"],
                    "warrant_bid": warrant_bid_disp,
                    "opt_bid": opt_bid_per_share,
                    "opt_ask": opt_ask_per_share,
                    "warrant_per_share": exec_warrant,
                    "opt_per_share": exec_opt,
                    "price_diff": price_diff,
                    "price_diff_pct": price_diff_pct,
                    "warrant_iv": warrant_iv,
                    "opt_iv": opt_iv,
                    "iv_diff": round((opt_iv or 0) - (warrant_iv or 0), 4),
                })
    return rows


def _match_warrants_pcp(warrant_df, opt_df, opt_contract_size,
                        max_strike_diff_pct, max_dte_diff,
                        positive_loose=False, r=None,
                        synthetic_underlying="warrant"):
    """Put-Call-Parity matcher: price a warrant against the SYNTHETIC built from
    the OPPOSITE-type option plus the underlying and a risk-free bond.

    European PCP (no dividends): C - P = S - K*e^(-rT), so
        synthetic call = P + S - K*e^(-rT)
        synthetic put  = C - S + K*e^(-rT)
    using the OPTION's strike Ko, expiry T, and discount rate ``r``
    (defaults to ``options_logic.R``).

    ``synthetic_underlying`` selects the spot S plugged into the synthetic:
      - "warrant": use the warrant's own underlying (single-market TAIFEX PCP —
        both legs share the same underlying).
      - "option":  use the OPTION row's underlying — the cross-market case, where
        the option is a US ADR converted to TWD/TW-share. The ADR-converted spot
        differs from the warrant's local spot by the ADR premium, which is the
        whole edge; the synthetic (put + ADR + USD bond) must be priced off the
        ADR, so pass ``r = us_options_logic.R_US`` too. The emitted row keeps the
        warrant's LOCAL underlying in ``underlying_price`` (premium baseline +
        warrant payoff chart) and carries the ADR spot in ``adr_underlying``.

    Two directions per pair:
      - EXECUTABLE  (long warrant / short synthetic): buy warrant@ask, sell the
        option@bid, short the stock (call) / long the stock (put), lend/borrow
        PV(Ko). Kept only when the warrant is cheap vs synthetic (price_diff>0)
        AND the guards hold (short option expires no later than the long warrant,
        and the strike gap is on the no-downside side). This is the ONLY tradable
        side (never shorts the warrant), so ``positive_loose`` applies here only:
        loose prices the two tradable quotes at their favorable side — buy
        warrant@bid, sell option@ask (spread not covered) — mirroring the Direct
        Match positive-loose leg. Stock/bond legs are theoretical and unchanged.
      - NON-EXECUTABLE (short warrant / long synthetic): warrants can't be
        shorted — emitted for debugging only, flagged executable=False, guards
        skipped. Kept when the warrant is rich vs synthetic (price_diff<0).
    """
    rows = []
    if r is None:
        r = options_logic.R
    s_from_option = synthetic_underlying == "option"

    for _, w in warrant_df.iterrows():
        opp = "Put" if w["type"] == "Call" else "Call"
        candidates = opt_df[opt_df["type"] == opp].copy()
        if candidates.empty:
            continue

        ratio = float(w["exercise_ratio"])
        if ratio <= 0:
            continue  # can't size or normalise a warrant with no exercise ratio

        candidates["strike_diff_pct"] = (
            (candidates["strike"] - w["strike"]).abs() / w["strike"] * 100
        )
        candidates["dte_diff"] = (
            (candidates["days_to_expiry"] - w["days_to_expiry"]).abs()
        )
        candidates = candidates[candidates["strike_diff_pct"] <= max_strike_diff_pct]
        if candidates.empty:
            continue

        S_local = float(w["underlying_price"])   # warrant's own (local) spot
        Kw = float(w["strike"])
        is_call = w["type"] == "Call"
        warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
        warrant_bid_val = float(w["bid"]) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else float(w["ask"])
        warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
        warrants_needed = round(opt_contract_size / ratio)
        warrant_bid_disp = round(float(w["bid"]), 4) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else None

        # Warrant-leg IV (display only; payoff is intrinsic). Use warrant type.
        if pd.notna(w.get("iv_ask")):
            warrant_iv = round(float(w["iv_ask"]), 4)
        else:
            _wiv = warrant_logic.implied_vol(
                float(w["ask"]), S_local, Kw,
                int(w["days_to_expiry"]) / 365.0, r, ratio, not is_call)
            warrant_iv = round(float(_wiv), 4) if pd.notna(_wiv) and 0 < float(_wiv) <= 3 else None

        for _, opt in candidates.iterrows():
            # Spot for the synthetic: the option's underlying in the cross-market
            # case (ADR-converted), else the warrant's own local spot.
            S = float(opt["underlying_price"]) if s_from_option else S_local
            Ko = float(opt["strike"])
            To = int(opt["days_to_expiry"]) / 365.0
            bond_pv = round(Ko * float(np.exp(-r * To)), 4)
            # Gate on per-side liveness flagged BEFORE the settlement/last
            # fallback: opt["bid"]/["ask"] are post-fallback, so a missing side
            # carries a stale settlement mark that is not an executable price.
            opt_bid_ps = round(float(opt["bid"]), 4) if bool(opt.get("bid_live", False)) else None
            opt_ask_ps = round(float(opt["ask"]), 4) if bool(opt.get("ask_live", False)) else None

            # Option-leg IV (display only) — use the OPTION's put/call flag.
            is_put_opt = opp == "Put"
            if pd.notna(opt.get("iv_bid")):
                opt_iv = round(float(opt["iv_bid"]), 4)
            else:
                _omid = None
                if opt_bid_ps is not None and opt_ask_ps is not None:
                    _omid = (opt_bid_ps + opt_ask_ps) / 2
                elif opt_ask_ps is not None:
                    _omid = opt_ask_ps
                _oiv = warrant_logic.implied_vol(
                    _omid, S, Ko, To, r, 1.0, is_put_opt) if _omid else None
                opt_iv = round(float(_oiv), 4) if (_oiv is not None and pd.notna(_oiv) and 0 < float(_oiv) <= 3) else None

            def _synth(opt_ps):
                # synthetic call = P + S - PV(K);  synthetic put = C - S + PV(K)
                return round((opt_ps + S - bond_pv) if is_call else (opt_ps - S + bond_pv), 4)

            def _emit(executable, opt_ps, warrant_ps, price_diff):
                synthetic_price = _synth(opt_ps)
                pct = round(price_diff / synthetic_price * 100, 2) if synthetic_price > 0 else None
                # Executable side buys the warrant (lifts ask -> SELL size gates);
                # the non-executable debug side would short it (hits bid).
                warrant_depth_lots = int(w.get("ask_qty") or 0) if executable else int(w.get("bid_qty") or 0)
                board_lots_needed = round(warrants_needed / 1000, 4)
                fillable = warrant_depth_lots >= board_lots_needed
                if executable:
                    if is_call:
                        trade = "Buy Call Warrant / Short Synthetic (short Put + short stock + lend)"
                    else:
                        trade = "Buy Put Warrant / Short Synthetic (short Call + long stock + borrow)"
                else:
                    side = "short Put + short stock + lend" if is_call else "short Call + long stock + borrow"
                    trade = f"Short {w['type']} Warrant / Long Synthetic ({side}) — NON-EXECUTABLE"
                rows.append({
                    "warrant_code": w["warrant_code"],
                    "warrant_name": w["warrant_name"],
                    "option_contract": opt["contract"],
                    "type": w["type"],
                    "opt_type": opp,
                    "trade": trade,
                    "executable": bool(executable),
                    "underlying_price": round(S_local, 4),
                    "adr_underlying": round(S, 4),
                    "warrant_dte": int(w["days_to_expiry"]),
                    "opt_dte": int(opt["days_to_expiry"]),
                    "dte_diff": int(opt["dte_diff"]),
                    "warrant_strike": round(Kw, 2),
                    "opt_strike": round(Ko, 2),
                    "strike_diff_pct": round(float(opt["strike_diff_pct"]), 2),
                    "warrants_needed": warrants_needed,
                    "board_lots": board_lots_needed,
                    "warrant_depth_lots": warrant_depth_lots,
                    "fillable": bool(fillable),
                    "opt_contract_size": opt_contract_size,
                    "warrant_ask": round(float(w["ask"]), 4),
                    "warrant_bid": warrant_bid_disp,
                    "opt_bid": opt_bid_ps,
                    "opt_ask": opt_ask_ps,
                    "warrant_per_share": warrant_ps,
                    "opt_per_share": round(opt_ps, 4),
                    "synthetic_price": synthetic_price,
                    "bond_pv": bond_pv,
                    "price_diff": round(price_diff, 4),
                    "price_diff_pct": pct,
                    "warrant_iv": warrant_iv,
                    "opt_iv": opt_iv,
                    "iv_diff": round((opt_iv or 0) - (warrant_iv or 0), 4),
                })

            # Executable: long warrant / short synthetic. Only tradable side, so
            # the loose toggle applies here (and here only).
            #   tight : buy warrant@ask, sell option@bid (real executable fills)
            #   loose : buy warrant@bid, sell option@ask (favorable side)
            exec_opt_ps = opt_ask_ps if positive_loose else opt_bid_ps
            exec_warrant_ps = warrant_bid_per_share if positive_loose else warrant_ask_per_share
            if exec_opt_ps is not None:
                price_diff = round(_synth(exec_opt_ps) - exec_warrant_ps, 4)
                # Guards: short option must not outlive the long warrant, and the
                # strike gap must be on the no-downside side (Call: Ko>=Kw, Put:
                # Ko<=Kw) so the residual is a bounded, never-negative vertical.
                dte_ok = int(opt["days_to_expiry"]) <= int(w["days_to_expiry"])
                strike_ok = (Ko >= Kw) if is_call else (Ko <= Kw)
                if price_diff > 0 and dte_ok and strike_ok:
                    _emit(True, exec_opt_ps, exec_warrant_ps, price_diff)

            # Non-executable debug: short warrant (sell@bid) vs long synthetic
            # (buy opt@ask). No guards — warrants aren't shortable anyway.
            if opt_ask_ps is not None:
                price_diff = round(_synth(opt_ask_ps) - warrant_bid_per_share, 4)
                if price_diff < 0:
                    _emit(False, opt_ask_ps, warrant_bid_per_share, price_diff)

    return rows


def _match_butterflies(warrant_df, opt_df, opt_contract_size, positive_loose=False):
    """Convexity (butterfly) arb: LONG two warrant wings + SHORT 2x an option body.

    A same-type butterfly across three strikes K1 < x < K2 pays off >= 0 at every
    spot yet can be entered for a NET CREDIT when the chain violates convexity
    (the body is too dear relative to the wings). Neither Direct Match (a single
    2-leg vertical vs its own bound) nor PCP (single-strike parity) can see this —
    the mispricing lives in the RELATIONSHIP between two adjacent verticals, i.e.
    the second difference of price across three strikes.

    Construction (per the spec):
      - Wings  = two LONG warrants at K1 and K2 (warrants can't be shorted, and a
        fly's wings are long anyway). One cheapest warrant is taken per strike.
      - Body   = SHORT 2 contracts of the single most-expensive-to-sell OPTION
        whose strike sits strictly between the wings (K1 < x < K2). Shorting 2x
        the body flattens both tails, so the payoff is bounded on both ends.
      - Cover  = the short body must expire no later than the SHORTER-dated wing,
        so both long wings are still alive to cover assignment on the body. The
        wings then carry residual time value (>= intrinsic), so pricing the floor
        at pure intrinsic is conservative.

    Sizing is anchored to the body: short 2 contracts = 2 * opt_contract_size
    shares, and each wing is bought to opt_contract_size underlying shares
    (warrants_needed = opt_contract_size / exercise_ratio), so the per-underlying-
    share weights are the classic 1 : -2 : 1.

    Executable (tight) prices: BUY wings @ ask, SELL body @ bid.
    ``positive_loose`` (DEBUG, mirrors Direct Match) prices the FAVORABLE side —
    BUY wings @ bid, SELL body @ ask — which does not cross the spread and is not
    tradeable; it only enlarges the candidate pool to confirm wiring.

    Terminal payoff floor per underlying share (intrinsic, all same type):
        credit_ps      = 2*body_ps - wing_lo_ps - wing_hi_ps
        tail (call)    = 2*x - K1 - K2      (flat far-upside level)
        tail (put)     = K1 + K2 - 2*x      (flat far-downside level)
        worst_payoff   = min(0, tail)       (the other tail is 0)
        guaranteed_ps  = worst_payoff + credit_ps
    The row is a locked arb iff guaranteed_ps > 0; only such rows are emitted.
    """
    rows = []
    M = int(opt_contract_size)   # underlying shares the structure is scaled to

    for typ in ("Call", "Put"):
        is_call = typ == "Call"
        warr = warrant_df[warrant_df["type"] == typ]
        opts = opt_df[opt_df["type"] == typ]
        if warr.empty or opts.empty:
            continue

        # Collapse warrants to the single cheapest-to-BUY wing per strike.
        # buy side = ask (tight) / bid (loose); price per underlying share.
        wings = {}
        for _, w in warr.iterrows():
            ratio = float(w["exercise_ratio"] or 0)
            ask = float(w["ask"] or 0) if pd.notna(w.get("ask")) else 0.0
            bid = float(w["bid"] or 0) if pd.notna(w.get("bid")) else 0.0
            if ratio <= 0 or ask <= 0:
                continue
            if positive_loose and bid <= 0:
                continue
            buy_raw = bid if positive_loose else ask
            buy_ps = round(buy_raw / ratio, 6)
            K = round(float(w["strike"]), 4)
            cur = wings.get(K)
            if cur is None or buy_ps < cur["buy_ps"]:
                wings[K] = {
                    "K": K, "buy_ps": buy_ps, "ratio": ratio,
                    "ask": ask, "bid": bid if bid > 0 else None,
                    "dte": int(w["days_to_expiry"]),
                    "code": str(w["warrant_code"]), "name": w["warrant_name"],
                    "depth_lots": int(w.get("ask_qty") or 0),
                    "S": float(w["underlying_price"]),
                }

        # Sellable body options: sell side = bid (tight) / ask (loose).
        bodies = []
        for _, o in opts.iterrows():
            bid = float(o["bid"] or 0) if pd.notna(o.get("bid")) else 0.0
            ask = float(o["ask"] or 0) if pd.notna(o.get("ask")) else 0.0
            sell_raw = ask if positive_loose else bid
            if sell_raw <= 0:
                continue
            bodies.append({
                "K": round(float(o["strike"]), 4), "sell_ps": round(sell_raw, 6),
                "bid": bid if bid > 0 else None, "ask": ask if ask > 0 else None,
                "dte": int(o["days_to_expiry"]),
                "contract": str(o["contract"]),
                "vol": int(o.get("volume") or 0),
                "S": float(o["underlying_price"]),
            })
        if len(wings) < 2 or not bodies:
            continue

        strikes = sorted(wings)
        for a in range(len(strikes)):
            for b in range(a + 1, len(strikes)):
                w1, w2 = wings[strikes[a]], wings[strikes[b]]
                K1, K2 = w1["K"], w2["K"]
                dte_cap = min(w1["dte"], w2["dte"])
                # Most-expensive-to-sell body strictly between the wings, expiring
                # no later than the shorter-dated wing.
                cands = [bd for bd in bodies
                         if K1 < bd["K"] < K2 and bd["dte"] <= dte_cap]
                if not cands:
                    continue
                body = max(cands, key=lambda bd: bd["sell_ps"])
                x = body["K"]

                credit_ps = round(2 * body["sell_ps"] - w1["buy_ps"] - w2["buy_ps"], 6)
                tail = (2 * x - K1 - K2) if is_call else (K1 + K2 - 2 * x)
                worst_payoff_ps = min(0.0, tail)
                guaranteed_ps = round(worst_payoff_ps + credit_ps, 6)
                if guaranteed_ps <= 0:
                    continue  # not a locked arb — skip

                max_loss_ps = round(max(0.0, -guaranteed_ps), 6)
                wing_lo_units = round(M / w1["ratio"])
                wing_hi_units = round(M / w2["ratio"])
                lo_lots = round(wing_lo_units / 1000, 4)
                hi_lots = round(wing_hi_units / 1000, 4)
                lo_fill = w1["depth_lots"] >= lo_lots
                hi_fill = w2["depth_lots"] >= hi_lots
                cost_basis_ps = w1["buy_ps"] + w2["buy_ps"]
                S = w1["S"]

                rows.append({
                    "underlying_code": None,  # set by caller
                    "type": typ,
                    "trade": "Long Wings / Short 2x Body",
                    "underlying_price": round(S, 4),
                    # wings (long warrants)
                    "wing_lo_code": w1["code"], "wing_lo_name": w1["name"],
                    "wing_lo_strike": K1, "wing_lo_dte": w1["dte"],
                    "wing_lo_ratio": w1["ratio"], "wing_lo_ask": round(w1["ask"], 4),
                    "wing_lo_bid": (round(w1["bid"], 4) if w1["bid"] else None),
                    "wing_lo_ps": round(w1["buy_ps"], 4),
                    "wing_lo_units": wing_lo_units, "wing_lo_lots": lo_lots,
                    "wing_lo_depth_lots": w1["depth_lots"], "wing_lo_fillable": bool(lo_fill),
                    "wing_hi_code": w2["code"], "wing_hi_name": w2["name"],
                    "wing_hi_strike": K2, "wing_hi_dte": w2["dte"],
                    "wing_hi_ratio": w2["ratio"], "wing_hi_ask": round(w2["ask"], 4),
                    "wing_hi_bid": (round(w2["bid"], 4) if w2["bid"] else None),
                    "wing_hi_ps": round(w2["buy_ps"], 4),
                    "wing_hi_units": wing_hi_units, "wing_hi_lots": hi_lots,
                    "wing_hi_depth_lots": w2["depth_lots"], "wing_hi_fillable": bool(hi_fill),
                    # body (short 2 option contracts)
                    "mid_contract": body["contract"], "mid_strike": x,
                    "mid_dte": body["dte"], "mid_contract_size": M,
                    "mid_bid": body["bid"], "mid_ask": body["ask"],
                    "mid_ps": round(body["sell_ps"], 4), "mid_contracts": 2,
                    "mid_volume": body["vol"],
                    # economics (per underlying share)
                    "credit_ps": round(credit_ps, 4),
                    "tail_ps": round(float(tail), 4),
                    "worst_payoff_ps": round(worst_payoff_ps, 4),
                    "guaranteed_ps": round(guaranteed_ps, 4),
                    "max_loss_per_share": max_loss_ps,
                    # totals (NT$; structure scaled to M underlying shares)
                    "net_credit": round(credit_ps * M, 0),
                    "guaranteed_profit": round(guaranteed_ps * M, 0),
                    "max_loss": round(max_loss_ps * M, 0),
                    "riskless": True,
                    "fillable": bool(lo_fill and hi_fill),
                    "board_lots": round(lo_lots + hi_lots, 4),
                    # headline edge for the compact table
                    "price_diff": round(guaranteed_ps, 4),
                    "price_diff_pct": (round(guaranteed_ps / cost_basis_ps * 100, 2)
                                       if cost_basis_ps > 0 else None),
                    "loose_prices": bool(positive_loose),
                })

    return rows


def match_warrant_tw_option(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                  positive_loose=False, min_volume=0, strategy="same_type"):
    stock_codes = list(stock_codes)

    # PCP pairs a warrant with the OPPOSITE-type option; butterfly pairs each
    # type against its own body option. Both want the full chain regardless of
    # the warrant filter, so fetch every type and filter downstream.
    opt_type_fetch = "All" if strategy in ("pcp", "butterfly") else option_type

    # Fetch ONCE for every selected code, not per-code: read_warrant/read_tw_option
    # already accept a list and the underlying Supabase snapshot read
    # (db_market.read_snapshot) already filters server-side on `codes` in one
    # query — the old per-code loop paid a redundant md_batches round-trip +
    # paginated read for every single selected stock, so wall time scaled with
    # product count instead of being ~constant. No time-value cap / IV solve on
    # the arb path: a positive price arb only needs warrant ask + option bid, so
    # nothing should drop a leg over time value or a non-converging IV.
    all_warrant_df, warrant_err, warrant_meta = (
        warrant_logic.read_warrant(stock_codes, option_type, 0, 365, 0, 1e9, 0, compute_iv=False)
        if stock_codes else (pd.DataFrame(), None, None)
    )
    all_opt_df, opt_err, opt_meta = (
        options_logic.read_tw_option(stock_codes, opt_type_fetch, min_days=1, compute_iv=False)
        if stock_codes else (pd.DataFrame(), None, None)
    )
    # A fetch that actually failed (CMoney/TAIFEX down) is an error for every
    # code in the scan; an empty frame with no hard_error just means there was
    # nothing to read.
    warrant_hard = (warrant_meta or {}).get("hard_error")
    opt_hard = (opt_meta or {}).get("hard_error")
    if not all_opt_df.empty:
        all_opt_df = all_opt_df[all_opt_df["ask_live"] | all_opt_df["bid_live"]]
        if min_volume > 0:
            all_opt_df = all_opt_df[all_opt_df["volume"] >= min_volume]

    def _process(i, code):
        """Match one stock code against the pre-fetched frames.

        Returns (rows, skip_reason_or_None, hard_error_or_None).
        """
        pos = f"({i}/{len(stock_codes)})"
        if code not in options_logic._commodity_map():
            applog.log("ARB", f"{code} {pos} skipped: no options data available")
            return [], f"{code}: no options data available", None

        cfg = options_logic._commodity_map()[code]
        opt_contract_size = cfg["exercise_ratio"]

        warrant_df = (
            all_warrant_df[all_warrant_df["underlying_code"].astype(str) == str(code)]
            if not all_warrant_df.empty else all_warrant_df
        )
        if warrant_df.empty:
            msg = f"{code}: {warrant_err or 'no warrants'}"
            applog.log("ARB", f"{code} {pos} skipped: {warrant_err or 'no warrants'}",
                       level="ERROR" if warrant_hard else "INFO")
            return ([], None, msg) if warrant_hard else ([], msg, None)

        opt_df = (
            all_opt_df[all_opt_df["stock_code"].astype(str) == str(code)]
            if not all_opt_df.empty else all_opt_df
        )
        if opt_df.empty:
            msg = f"{code}: {opt_err or 'no options'}"
            applog.log("ARB", f"{code} {pos} skipped: no live options",
                       level="ERROR" if opt_hard else "INFO")
            return ([], None, msg) if opt_hard else ([], msg, None)

        if strategy == "pcp":
            rows = _match_warrants_pcp(
                warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose,
            )
        elif strategy == "butterfly":
            rows = _match_butterflies(
                warrant_df, opt_df, opt_contract_size, positive_loose=positive_loose,
            )
        else:
            rows = _match_warrants_to_options(
                warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose,
            )
        for r in rows:
            if r.get("underlying_code") is None:
                r["underlying_code"] = code
        applog.log(
            "ARB",
            f"{code} {pos} warrants={len(warrant_df)} options={len(opt_df)} "
            f"matched={len(rows)} strategy={strategy}",
        )
        return rows, None, None

    # Plain loop, not a thread pool: _process is now pure in-memory pandas
    # (the fetch happened once, above), so there is no I/O left to overlap —
    # a ThreadPoolExecutor here bought nothing but GIL contention that helped
    # peg the single-worker deploy's CPU cap.
    all_rows = []
    skip_reasons = []
    hard_errors = []
    for i, code in enumerate(stock_codes, 1):
        rows, skip, hard = _process(i, code)
        all_rows.extend(rows)
        if skip:
            skip_reasons.append(skip)
        if hard:
            hard_errors.append(hard)

    if not all_rows:
        if hard_errors:
            # A data source actually failed — a genuine error.
            raise RuntimeError("; ".join(hard_errors))
        # Every code was read and matched fine; the filter just passed nothing
        # through. That is a normal, common scan outcome (most of the time
        # there is no arb), not a failure — return an empty result instead of
        # raising so the caller doesn't render it as an error.
        if skip_reasons:
            applog.log("ARB", "no matches; " + "; ".join(skip_reasons))
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)
    if strategy == "pcp" and "executable" in result.columns:
        # Executable arbs first, then by richest mispricing.
        result = result.sort_values(
            ["executable", "price_diff_pct"], ascending=[False, False]
        )
    elif "price_diff_pct" in result.columns:
        if "riskless" in result.columns:
            # True arbs (residual vertical never clawed back at expiry) outrank
            # capped-loss pairs regardless of headline edge.
            result = result.sort_values(
                ["riskless", "price_diff_pct"], ascending=[False, False]
            )
        else:
            result = result.sort_values("price_diff_pct", ascending=False)
    return result


def match_warrant_us_option(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                       positive_loose=False, min_volume=0, strategy="same_type"):
    """Match Taiwan warrants against the same-underlying US ADR options.

    The US option leg is pre-converted to TWD-per-Taiwan-share by
    us_options_logic, so it lives in the same price space as the warrant leg.
    One US contract controls 100 * adr_ratio Taiwan shares (per listing), which
    drives warrants_needed = contract_size/ratio.

    strategy:
      - "same_type": direct match warrant to the SAME-type US option
        (_match_warrants_to_options).
      - "pcp": cross-market Put-Call Parity — price the warrant against the
        synthetic built from the OPPOSITE-type US option + the US ADR + a USD
        bond (_match_warrants_pcp with r=R_US, synthetic_underlying="option").
    """
    all_rows = []
    # Skip reasons never gate the raise; only genuine fetch failures do
    # (see match_warrant_tw_option).
    skip_reasons = []
    hard_errors = []

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in us_options_logic._adr_map():
            skip_reasons.append(f"{code}: no US ADR mapping")
            applog.log("ARB", f"{code} {pos} skipped: no US ADR mapping")
            continue

        contract_size = us_options_logic.contract_tw_shares(code)  # TW shares/contract

        # No time-value cap and no IV solve on the arb path (see match_warrant_tw_option).
        warrant_df, err, wmeta = warrant_logic.read_warrant(
            [code], option_type, 0, 365, 0, 1e9, 0, compute_iv=False
        )
        if warrant_df.empty:
            msg = f"{code}: {err or 'no warrants'}"
            hard = bool((wmeta or {}).get("hard_error"))
            (hard_errors if hard else skip_reasons).append(msg)
            applog.log("ARB", f"{code} {pos} skipped: {err or 'no warrants'}",
                       level="ERROR" if hard else "INFO")
            continue

        # PCP pairs a warrant with the OPPOSITE-type option, so the option
        # fetch must include both types regardless of the warrant filter.
        opt_type_fetch = "All" if strategy == "pcp" else option_type
        opt_df, opt_err, ometa = us_options_logic.read_us_option([code], opt_type_fetch, min_days=1, compute_iv=False)
        if opt_df.empty:
            msg = f"{code}: {opt_err or 'no US options'}"
            hard = bool((ometa or {}).get("hard_error"))
            (hard_errors if hard else skip_reasons).append(msg)
            applog.log("ARB", f"{code} {pos} no US options: {opt_err}",
                       level="ERROR" if hard else "INFO")
            continue
        opt_df = opt_df[opt_df["ask_live"] | opt_df["bid_live"]]
        if min_volume > 0:
            opt_df = opt_df[opt_df["volume"] >= min_volume]

        if opt_df.empty:
            skip_reasons.append(f"{code}: no live US options")
            applog.log("ARB", f"{code} {pos} skipped: no live US options")
            continue

        if strategy == "pcp":
            rows = _match_warrants_pcp(
                warrant_df, opt_df, contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose, r=us_options_logic.R_US,
                synthetic_underlying="option",
            )
        else:
            rows = _match_warrants_to_options(
                warrant_df, opt_df, contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose,
            )
        applog.log(
            "ARB",
            f"{code} {pos} warrants={len(warrant_df)} us_options={len(opt_df)} "
            f"matched={len(rows)} strategy={strategy}",
        )
        for r in rows:
            r["us_stock_code"] = code  # so the modal can pull ADR-premium history
        all_rows.extend(rows)

    if not all_rows:
        if hard_errors:
            raise RuntimeError("; ".join(hard_errors))
        # Every code was read and matched fine; the filter just passed nothing
        # through — a normal empty scan, not a failure. See match_warrant_tw_option.
        if skip_reasons:
            applog.log("ARB", "no matches; " + "; ".join(skip_reasons))
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)
    if strategy == "pcp" and "executable" in result.columns:
        # Executable arbs first, then by richest mispricing.
        result = result.sort_values(
            ["executable", "price_diff_pct"], ascending=[False, False]
        )
    elif "price_diff_pct" in result.columns:
        if "riskless" in result.columns:
            # True arbs (residual vertical never clawed back at expiry) outrank
            # capped-loss pairs regardless of headline edge.
            result = result.sort_values(
                ["riskless", "price_diff_pct"], ascending=[False, False]
            )
        else:
            result = result.sort_values("price_diff_pct", ascending=False)
    return result


def _lcm(a, b):
    from math import gcd
    return a * b // gcd(a, b)


def _match_option_legs(tw_df, us_df, tw_contract_shares, us_contract_shares,
                       max_strike_diff_pct, max_dte_diff, positive_loose=False):
    """Match a Taiwan listed option to the *nearest* US ADR option (same type,
    closest strike, then closest expiry) so the two legs share ~the same payoff
    and delta roughly cancels.

    The two legs are NOT 1:1 — the ADR trades at a premium and FX floats — so
    this is not a risk-free arb. The trade is: sell the richer leg, buy the
    cheaper, and hold the residual ADR-premium + FX basis. The entry credit
    (executable: sell@bid, buy@ask) is the headline; the true edge is the
    probability-weighted P&L over where the premium lands by expiry, computed in
    the modal from the historical premium distribution (same engine as the
    US Option Match tab). This function emits product-neutral ``tw_option_*`` /
    ``us_option_*`` keys (no warrant is involved in a TW-option-vs-US-option
    match); the shared frontend modal engine's translation layer maps the TW leg
    into its ``warrant_*`` slots and the US leg into its ``opt_*`` slots — those
    modal slot names are unchanged, since the other two match functions (which
    DO involve a real warrant) still populate them directly.
    """
    base = _lcm(int(tw_contract_shares), int(us_contract_shares))
    tw_contracts = base // int(tw_contract_shares)
    us_contracts = base // int(us_contract_shares)
    matched_shares = base

    rows = []
    for _, tw in tw_df.iterrows():
        cands = us_df[us_df["type"] == tw["type"]].copy()
        if cands.empty:
            continue

        cands["strike_diff_pct"] = (
            (cands["strike"] - tw["strike"]).abs() / tw["strike"] * 100
        )
        cands["dte_diff"] = (cands["days_to_expiry"] - tw["days_to_expiry"]).abs()
        cands = cands[
            (cands["strike_diff_pct"] <= max_strike_diff_pct)
            & (cands["dte_diff"] <= max_dte_diff)
        ]
        if cands.empty:
            continue

        # Best pair = closest strike, then closest expiry (delta-match priority).
        cands = cands.sort_values(["strike_diff_pct", "dte_diff"])
        us = cands.iloc[0]

        tw_bid = float(tw["bid"]) if pd.notna(tw.get("bid")) and float(tw.get("bid", 0)) > 0 else None
        tw_ask = float(tw["ask"]) if pd.notna(tw.get("ask")) and float(tw.get("ask", 0)) > 0 else None
        us_bid = float(us["bid"]) if pd.notna(us.get("bid")) and float(us.get("bid", 0)) > 0 else None
        us_ask = float(us["ask"]) if pd.notna(us.get("ask")) and float(us.get("ask", 0)) > 0 else None
        if None in (tw_bid, tw_ask, us_bid, us_ask):
            continue

        # Sell the richer leg (by mid), buy the cheaper. Executable prices.
        tw_mid = (tw_bid + tw_ask) / 2
        us_mid = (us_bid + us_ask) / 2
        if us_mid >= tw_mid:
            # US richer → Short US / Long TW: sell US@bid, buy TW@ask
            trade = "Long TW / Short US"
            exec_opt, exec_warrant = us_bid, tw_ask   # opt slot = US, warrant slot = TW
        else:
            # TW richer → Short TW / Long US: sell TW@bid, buy US@ask
            trade = "Long US / Short TW"
            exec_opt, exec_warrant = us_ask, tw_bid

        # The short leg must expire no later than the long leg. If the short
        # leg expired first, the long leg would be gone while the short lives
        # on — a naked short option, which is exactly the risk we refuse to
        # carry. Short = US when "Long TW / Short US", else TW.
        short_dte = int(us["days_to_expiry"]) if trade == "Long TW / Short US" else int(tw["days_to_expiry"])
        long_dte = int(tw["days_to_expiry"]) if trade == "Long TW / Short US" else int(us["days_to_expiry"])
        if short_dte > long_dte:
            continue  # would leave a naked short leg after the long expires

        # Strike must be on the FAVORABLE side or the pair is just a vertical
        # spread with a real max loss, not a no-downside structure. With a
        # received credit the no-loss vertical requires:
        #   Call: short strike >= long strike (short the higher call)
        #   Put:  short strike <= long strike (short the lower put)
        # Anything else has min payoff = credit − (unfavorable gap) < 0.
        short_strike = float(us["strike"]) if trade == "Long TW / Short US" else float(tw["strike"])
        long_strike = float(tw["strike"]) if trade == "Long TW / Short US" else float(us["strike"])
        if tw["type"] == "Put":
            if short_strike > long_strike:
                continue  # short the higher put -> downside loss
        else:
            if short_strike < long_strike:
                continue  # short the lower call -> downside loss

        # Entry credit per share (sell price − buy price), sign per direction.
        # pcp_diff sign drives the modal payoff direction: >0 long-TW/short-US.
        if trade == "Long TW / Short US":
            credit = round(exec_opt - exec_warrant, 4)     # us_bid − tw_ask
        else:
            credit = round(exec_warrant - exec_opt, 4)     # tw_bid − us_ask
        if credit <= 0:
            continue  # no executable entry credit
        pcp_diff = credit if trade == "Long TW / Short US" else -credit

        # IV for each leg (matched pairs only, so cheap) — the modal needs it to
        # mark the not-yet-expired leg with time value at the trade horizon, so
        # the scenario P&L varies with the premium instead of being flat.
        is_put = tw["type"] == "Put"
        tw_iv = warrant_logic.implied_vol(
            tw_mid, float(tw["underlying_price"]), float(tw["strike"]),
            int(tw["days_to_expiry"]) / 365.0, options_logic.R, 1.0, is_put)
        us_iv = warrant_logic.implied_vol(
            us_mid, float(us["underlying_price"]), float(us["strike"]),
            int(us["days_to_expiry"]) / 365.0, us_options_logic.R_US, 1.0, is_put)
        tw_iv = round(float(tw_iv), 4) if pd.notna(tw_iv) and 0 < float(tw_iv) <= 3 else None
        us_iv = round(float(us_iv), 4) if pd.notna(us_iv) and 0 < float(us_iv) <= 3 else None

        # Orderbook depth per leg. TW leg: best-level size (口) from TAIFEX MIS on
        # the side actually hit (buy TW@ask -> ask_size; sell TW@bid -> bid_size).
        # US leg: yfinance exposes no bid/ask size, so volume + OI stand in as a
        # liquidity proxy (NOT true resting depth).
        tw_size = tw.get("ask_size") if trade == "Long TW / Short US" else tw.get("bid_size")
        tw_size = int(tw_size) if pd.notna(tw_size) else None
        tw_fillable = tw_size is not None and tw_size >= int(tw_contracts)
        us_vol = int(us.get("volume") or 0)
        us_oi = int(us.get("oi") or 0)

        denom = exec_opt if exec_opt else 1
        rows.append({
            "tw_option_code": tw["contract"],
            "tw_option_name": f"TW {tw['contract']}",
            "us_option_contract": us["contract"],
            "type": tw["type"],
            "tw_option_type": tw["type"],
            "us_option_type": us["type"],
            "trade": trade,
            "underlying_price": round(float(tw["underlying_price"]), 4),
            "tw_option_dte": int(tw["days_to_expiry"]),
            "us_option_dte": int(us["days_to_expiry"]),
            "dte_diff": int(us["dte_diff"]),
            "tw_option_strike": round(float(tw["strike"]), 2),
            "us_option_strike": round(float(us["strike"]), 2),
            "strike_diff_pct": round(float(us["strike_diff_pct"]), 2),
            "tw_contracts": int(tw_contracts),
            "us_contracts": int(us_contracts),
            "tw_depth_contracts": tw_size,
            "tw_fillable": bool(tw_fillable),
            "us_volume": us_vol,
            "us_oi": us_oi,
            "matched_shares": int(matched_shares),
            "tw_contracts_needed": int(matched_shares),
            "us_option_contract_size": int(matched_shares),
            "tw_option_ask": round(tw_ask, 4),
            "tw_option_bid": round(tw_bid, 4),
            "us_option_bid": round(us_bid, 4),
            "us_option_ask": round(us_ask, 4),
            "tw_option_per_share": round(exec_warrant, 4),
            "us_option_per_share": round(exec_opt, 4),
            "price_diff": pcp_diff,
            "price_diff_pct": round(credit / denom * 100, 2),
            "entry_credit": round(credit * matched_shares, 0),
            "tw_option_iv": tw_iv,
            "us_option_iv": us_iv,
            "iv_diff": round((us_iv or 0) - (tw_iv or 0), 4),
        })
    return rows


def match_tw_us_option(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                           positive_loose=False, min_volume=0):
    """Match Taiwan listed options against US ADR options on the same underlying."""
    all_rows = []
    # Skip reasons never gate the raise; only genuine fetch failures do
    # (see match_warrant_tw_option).
    skip_reasons = []
    hard_errors = []

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in options_logic._commodity_map():
            skip_reasons.append(f"{code}: no Taiwan options")
            applog.log("ARB", f"{code} {pos} skipped: no Taiwan options")
            continue
        if code not in us_options_logic._adr_map():
            skip_reasons.append(f"{code}: no US ADR options")
            applog.log("ARB", f"{code} {pos} skipped: no US ADR options")
            continue

        tw_contract_shares = options_logic._commodity_map()[code]["exercise_ratio"]  # 2000
        us_contract_shares = us_options_logic.contract_tw_shares(code)            # 500 (2303)

        # Like US Option Match's warrant leg: don't require a live two-sided
        # quote on the TW option leg — fall back to the last settlement
        # snapshot so the scan still works when TAIFEX is closed. (Off-hours
        # prices are stale marks, not executable until the market reopens.)
        tw_df, tw_err, tw_meta = options_logic.read_tw_option([code], option_type, min_days=1, compute_iv=False)
        if tw_df.empty:
            msg = f"{code}: TW options {tw_err or 'no data'}"
            hard = bool((tw_meta or {}).get("hard_error"))
            (hard_errors if hard else skip_reasons).append(msg)
            applog.log("ARB", f"{code} {pos} no TW options: {tw_err}",
                       level="ERROR" if hard else "INFO")
            continue
        if min_volume > 0:
            tw_df = tw_df[tw_df["volume"] >= min_volume]

        us_df, us_err, us_meta = us_options_logic.read_us_option([code], option_type, min_days=1, compute_iv=False)
        if us_df.empty:
            msg = f"{code}: US options {us_err or 'no data'}"
            hard = bool((us_meta or {}).get("hard_error"))
            (hard_errors if hard else skip_reasons).append(msg)
            applog.log("ARB", f"{code} {pos} no US options: {us_err}",
                       level="ERROR" if hard else "INFO")
            continue
        us_df = us_df[us_df["is_live"]]
        if min_volume > 0:
            us_df = us_df[us_df["volume"] >= min_volume]

        if tw_df.empty or us_df.empty:
            skip_reasons.append(f"{code}: no live options on one leg")
            applog.log("ARB", f"{code} {pos} skipped: no live options on one leg")
            continue

        rows = _match_option_legs(
            tw_df, us_df, tw_contract_shares, us_contract_shares,
            max_strike_diff_pct, max_dte_diff, positive_loose=positive_loose,
        )
        applog.log(
            "ARB",
            f"{code} {pos} tw_options={len(tw_df)} us_options={len(us_df)} "
            f"matched={len(rows)}",
        )
        for r in rows:
            r["us_stock_code"] = code
        all_rows.extend(rows)

    if not all_rows:
        if hard_errors:
            raise RuntimeError("; ".join(hard_errors))
        # Every code was read and matched fine; the filter just passed nothing
        # through — a normal empty scan, not a failure. See match_warrant_tw_option.
        if skip_reasons:
            applog.log("ARB", "no matches; " + "; ".join(skip_reasons))
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)
    if "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result
