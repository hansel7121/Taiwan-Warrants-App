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

            # Positive: opt_strike >= warrant_strike (buy warrant, sell option)
            # Negative: opt_strike <= warrant_strike (buy option, sell warrant)
            strike_filter = (
                candidates["strike"] >= w["strike"]
                if direction == "positive"
                else candidates["strike"] <= w["strike"]
            )

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

            candidates = candidates[
                (candidates["strike_diff_pct"] <= max_strike_diff_pct)
                & dte_ok
                & strike_filter
            ]
            if candidates.empty:
                continue

            ratio = float(w["exercise_ratio"])
            if ratio <= 0:
                continue  # can't size or normalise a warrant with no exercise ratio
            warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
            warrant_bid_val = float(w["bid"]) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else float(w["ask"])
            warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
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
                if pd.isna(opt.get("ask")) or float(opt["ask"]) <= 0:
                    continue
                opt_bid_per_share = round(float(opt["bid"]), 4) if pd.notna(opt.get("bid")) and float(opt.get("bid", 0)) > 0 else None
                opt_ask_per_share = round(float(opt["ask"]), 4)

                # Positive tight (executable): opt_bid - warrant_ask > 0
                # Positive loose: opt_ask - warrant_bid > 0 (mirrors negative formula)
                # Negative (always loose): opt_bid - warrant_ask < 0
                if direction == "positive":
                    if positive_loose:
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
                    if opt_bid_per_share is None:
                        continue
                    price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)
                    exec_opt = opt_bid_per_share
                    exec_warrant = warrant_ask_per_share
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
                    _omid = None
                    if pd.notna(opt.get("bid")) and pd.notna(opt.get("ask")) and float(opt["bid"]) > 0 and float(opt["ask"]) > 0:
                        _omid = (float(opt["bid"]) + float(opt["ask"])) / 2
                    elif pd.notna(opt.get("ask")) and float(opt["ask"]) > 0:
                        _omid = float(opt["ask"])
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
            opt_bid_ps = round(float(opt["bid"]), 4) if pd.notna(opt.get("bid")) and float(opt.get("bid", 0)) > 0 else None
            opt_ask_ps = round(float(opt["ask"]), 4) if pd.notna(opt.get("ask")) and float(opt.get("ask", 0)) > 0 else None

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


def match_warrant_tw_option(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                  positive_loose=False, min_volume=0, strategy="same_type"):
    all_rows = []
    errors = []

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in options_logic.COMMODITY_MAP:
            errors.append(f"{code}: no options data available")
            applog.log("ARB", f"{code} {pos} skipped: no options data available")
            continue

        cfg = options_logic.COMMODITY_MAP[code]
        opt_contract_size = cfg["exercise_ratio"]

        # No time-value cap and no IV solve on the arb path: a positive price
        # arb only needs warrant ask + option bid, so nothing should drop a leg
        # over time value or a non-converging IV.
        warrant_df, err, _meta = warrant_logic.read_warrant(
            [code], option_type, 0, 365, 0, 1e9, 0, compute_iv=False
        )
        if warrant_df.empty:
            errors.append(f"{code}: {err or 'no warrants'}")
            applog.log("ARB", f"{code} {pos} skipped: {err or 'no warrants'}")
            continue

        # PCP pairs a warrant with the OPPOSITE-type option, so the option
        # fetch must include both types regardless of the warrant filter.
        opt_type_fetch = "All" if strategy == "pcp" else option_type
        opt_df, opt_err, _meta = options_logic.read_tw_option([code], opt_type_fetch, min_days=1, compute_iv=False)
        if opt_df.empty:
            errors.append(f"{code}: {opt_err or 'no options'}")
            applog.log("ARB", f"{code} {pos} option fetch failed: {opt_err}")
            continue
        opt_df = opt_df[opt_df["is_live"]]
        if min_volume > 0:
            opt_df = opt_df[opt_df["volume"] >= min_volume]

        if opt_df.empty:
            errors.append(f"{code}: no live options")
            applog.log("ARB", f"{code} {pos} skipped: no live options")
            continue

        if strategy == "pcp":
            rows = _match_warrants_pcp(
                warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose,
            )
        else:
            rows = _match_warrants_to_options(
                warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose,
            )
        applog.log(
            "ARB",
            f"{code} {pos} warrants={len(warrant_df)} options={len(opt_df)} "
            f"matched={len(rows)} strategy={strategy}",
        )
        all_rows.extend(rows)

    if not all_rows:
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if strategy == "pcp" and "executable" in result.columns:
        # Executable arbs first, then by richest mispricing.
        result = result.sort_values(
            ["executable", "price_diff_pct"], ascending=[False, False]
        )
    elif "price_diff_pct" in result.columns:
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
    errors = []

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in us_options_logic.US_ADR_MAP:
            errors.append(f"{code}: no US ADR mapping")
            applog.log("ARB", f"{code} {pos} skipped: no US ADR mapping")
            continue

        contract_size = us_options_logic.contract_tw_shares(code)  # TW shares/contract

        # No time-value cap and no IV solve on the arb path (see match_warrant_tw_option).
        warrant_df, err, _meta = warrant_logic.read_warrant(
            [code], option_type, 0, 365, 0, 1e9, 0, compute_iv=False
        )
        if warrant_df.empty:
            errors.append(f"{code}: {err or 'no warrants'}")
            applog.log("ARB", f"{code} {pos} skipped: {err or 'no warrants'}")
            continue

        # PCP pairs a warrant with the OPPOSITE-type option, so the option
        # fetch must include both types regardless of the warrant filter.
        opt_type_fetch = "All" if strategy == "pcp" else option_type
        opt_df, opt_err, _meta = us_options_logic.read_us_option([code], opt_type_fetch, min_days=1, compute_iv=False)
        if opt_df.empty:
            errors.append(f"{code}: {opt_err or 'no US options'}")
            applog.log("ARB", f"{code} {pos} US option fetch failed: {opt_err}")
            continue
        opt_df = opt_df[opt_df["is_live"]]
        if min_volume > 0:
            opt_df = opt_df[opt_df["volume"] >= min_volume]

        if opt_df.empty:
            errors.append(f"{code}: no live US options")
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
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if strategy == "pcp" and "executable" in result.columns:
        # Executable arbs first, then by richest mispricing.
        result = result.sort_values(
            ["executable", "price_diff_pct"], ascending=[False, False]
        )
    elif "price_diff_pct" in result.columns:
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
    errors = []

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in options_logic.COMMODITY_MAP:
            errors.append(f"{code}: no Taiwan options")
            applog.log("ARB", f"{code} {pos} skipped: no Taiwan options")
            continue
        if code not in us_options_logic.US_ADR_MAP:
            errors.append(f"{code}: no US ADR options")
            applog.log("ARB", f"{code} {pos} skipped: no US ADR options")
            continue

        tw_contract_shares = options_logic.COMMODITY_MAP[code]["exercise_ratio"]  # 2000
        us_contract_shares = us_options_logic.contract_tw_shares(code)            # 500 (2303)

        # Like US Option Match's warrant leg: don't require a live two-sided
        # quote on the TW option leg — fall back to the last settlement
        # snapshot so the scan still works when TAIFEX is closed. (Off-hours
        # prices are stale marks, not executable until the market reopens.)
        tw_df, tw_err, _meta = options_logic.read_tw_option([code], option_type, min_days=1, compute_iv=False)
        if tw_df.empty:
            errors.append(f"{code}: TW options {tw_err or 'no data'}")
            applog.log("ARB", f"{code} {pos} TW option fetch failed: {tw_err}")
            continue
        if min_volume > 0:
            tw_df = tw_df[tw_df["volume"] >= min_volume]

        us_df, us_err, _meta = us_options_logic.read_us_option([code], option_type, min_days=1, compute_iv=False)
        if us_df.empty:
            errors.append(f"{code}: US options {us_err or 'no data'}")
            applog.log("ARB", f"{code} {pos} US option fetch failed: {us_err}")
            continue
        us_df = us_df[us_df["is_live"]]
        if min_volume > 0:
            us_df = us_df[us_df["volume"] >= min_volume]

        if tw_df.empty or us_df.empty:
            errors.append(f"{code}: no live options on one leg")
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
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result
