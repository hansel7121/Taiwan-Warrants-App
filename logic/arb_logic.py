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
from logic.iv_engine import butterfly_pairs, direct_pairs


class NoMatchesError(RuntimeError):
    """Raised by an orchestrator when a scan completed cleanly but matched
    nothing — distinct from a hard data-source failure, which propagates as
    its own exception unmasked."""


def _finish_empty_scan(hard_errors, skip_reasons):
    """Bucket a per-code scan that produced no rows, and always raise.

    If a data source actually failed, that is a genuine error and propagates as
    a plain RuntimeError. Otherwise every code was read and matched fine and the
    filter simply passed nothing through — a normal, common scan outcome (most
    of the time there is no arb), not a failure — so it gets its own exception
    type that callers can catch narrowly, leaving genuine failures unmasked.

    Shared by all three orchestrators (match_warrant_tw_option,
    match_warrant_us_option, match_tw_us_option).
    """
    if hard_errors:
        raise RuntimeError("; ".join(hard_errors))
    if skip_reasons:
        applog.log("ARB", "no matches; " + "; ".join(skip_reasons))
    raise NoMatchesError(
        "no matches" + ("; " + "; ".join(skip_reasons) if skip_reasons else "")
    )


# ── Batched implied vol ──────────────────────────────────────────────────────
# The matchers below display an IV per leg. Solved per row inside the pairing
# loops that was up to 2*W*O scalar Brent solves per scan, each one its own
# Python->Rust FFI crossing, for values that depend on a single leg. These
# helpers solve each leg's IV once for the whole scan and hang the answer on the
# frame, so the pairing loops only ever read it.


def _flag(df, col):
    """A boolean column as a numpy array, False where the column is absent.

    Mirrors the row-level `bool(opt.get(col, False))` the loops used.
    """
    if col not in df.columns:
        return np.zeros(len(df), dtype=bool)
    return df[col].fillna(False).to_numpy(dtype=bool)


def _round4(v):
    """`round(x, 4)` on a possibly-NaN scalar, matching the per-row code exactly."""
    return round(float(v), 4)


def _iv_batch(price, S, K, dte, r, ratio, is_put):
    """`implied_vol` over whole columns, keeping the scalar callers' own guard.

    The per-row code kept a root only when `0 < iv <= 3` on the UNROUNDED value.
    The vectorized solver agrees with the scalar one to 4 dp, but its root can
    sit ~1e-6 away, so a root sitting exactly on a guard edge could be kept by
    one solver and dropped by the other. Those few rows are re-solved with the
    scalar solver; everything else takes the vector path.

    Returns a list of `round(iv, 4)` floats with `None` where the guard rejects
    — the same two-valued shape the loops emitted.
    """
    price = np.asarray(price, dtype=float)
    n = price.shape[0]
    if n == 0:
        return []
    S = np.broadcast_to(np.asarray(S, dtype=float), (n,))
    K = np.broadcast_to(np.asarray(K, dtype=float), (n,))
    T = np.asarray(dte, dtype=np.int64).astype(float) / 365.0
    ratio = np.broadcast_to(np.asarray(ratio, dtype=float), (n,))
    is_put = np.broadcast_to(np.asarray(is_put, dtype=bool), (n,))
    r_arr = np.broadcast_to(np.asarray(r, dtype=float), (n,))

    iv = warrant_logic.implied_vol_vec(price, S, K, T, r_arr, ratio, is_put)
    edge = np.isfinite(iv) & ((np.abs(iv - 3.0) < 1e-5) | (np.abs(iv) < 1e-5))
    for j in np.flatnonzero(edge):
        iv[j] = warrant_logic.implied_vol(
            float(price[j]), float(S[j]), float(K[j]), float(T[j]),
            float(r_arr[j]), float(ratio[j]), bool(is_put[j]))
    return [
        _round4(v) if (pd.notna(v) and 0 < float(v) <= 3) else None
        for v in iv
    ]


def _object_col(df, name, values):
    """Attach a column of `float | None` without pandas coercing None to NaN."""
    out = df.copy()
    out[name] = pd.Series(values, index=out.index, dtype=object)
    return out


def _with_warrant_iv(warrant_df, r):
    """Attach `_arb_warrant_iv`: the warrant leg's display IV, solved once.

    It depends only on the warrant's own quote and `r`, never on the option it
    is paired with or the direction being scanned, so the direction loop in
    `_match_warrants_to_options` was solving the identical value twice per
    warrant. A frame that already carries a solved `iv_ask` (the scanner's, not
    the arb path's) is used as-is, exactly as the per-row code did.
    """
    if warrant_df.empty:
        return warrant_df
    have = (warrant_df["iv_ask"].to_numpy() if "iv_ask" in warrant_df.columns
            else np.full(len(warrant_df), np.nan))
    need = pd.isna(have)
    ask = warrant_df["ask"].to_numpy(dtype=float)
    solved = _iv_batch(
        np.where(need, ask, np.nan),
        warrant_df["underlying_price"].to_numpy(dtype=float),
        warrant_df["strike"].to_numpy(dtype=float),
        warrant_df["days_to_expiry"].to_numpy(),
        r,
        warrant_df["exercise_ratio"].to_numpy(dtype=float),
        warrant_df["type"].to_numpy() == "Put",
    )
    vals = [s if n_ else _round4(h) for h, s, n_ in zip(have, solved, need)]
    return _object_col(warrant_df, "_arb_warrant_iv", vals)


def _live_per_share(opt_df):
    """(bid, ask) per share, `None` on a side with no live quote.

    `bid_live`/`ask_live` are flagged before the settlement/last fallback
    backfills a missing side, so they distinguish a real quote from a stale mark
    that `opt["bid"]`/`["ask"]` alone cannot.
    """
    bid = opt_df["bid"].to_numpy(dtype=float)
    ask = opt_df["ask"].to_numpy(dtype=float)
    bl = _flag(opt_df, "bid_live")
    al = _flag(opt_df, "ask_live")
    bid_ps = [_round4(bid[i]) if bl[i] else None for i in range(len(opt_df))]
    ask_ps = [_round4(ask[i]) if al[i] else None for i in range(len(opt_df))]
    return bid_ps, ask_ps


def _opt_iv_batch(opt_df, S, r, is_put, mid):
    """Option-leg display IV for a whole frame, honouring `if _omid` falsiness."""
    have = (opt_df["iv_bid"].to_numpy() if "iv_bid" in opt_df.columns
            else np.full(len(opt_df), np.nan))
    need = pd.isna(have)
    price = np.array([m if m else np.nan for m in mid], dtype=float)
    solved = _iv_batch(
        np.where(need, price, np.nan), S,
        opt_df["strike"].to_numpy(dtype=float),
        opt_df["days_to_expiry"].to_numpy(), r, 1.0, is_put,
    )
    return [s if n_ else _round4(h) for h, s, n_ in zip(have, solved, need)]


def _with_direct_opt_iv(opt_df):
    """Attach `_arb_opt_iv` for the same-type matcher.

    `candidates` is filtered to the warrant's own type, so the put/call flag the
    per-row solve used (`w["type"] == "Put"`) is always the OPTION's own type,
    and the mid it prices comes from the option's live sides alone. The value is
    therefore a property of the option row rather than of the pair — it was
    being re-solved up to 2*W times for the same option.
    """
    if opt_df.empty:
        return opt_df
    bid_ps, ask_ps = _live_per_share(opt_df)
    mid = []
    for b, a in zip(bid_ps, ask_ps):
        if b is not None and a is not None:
            mid.append((b + a) / 2)
        else:
            mid.append(a if a is not None else b)
    vals = _opt_iv_batch(opt_df, opt_df["underlying_price"].to_numpy(dtype=float),
                         options_logic.R, opt_df["type"].to_numpy() == "Put", mid)
    return _object_col(opt_df, "_arb_opt_iv", vals)


def _direct_option_arrays(opt_df):
    """Option columns as plain arrays, plus the row indices of each type.

    The matcher used to rebuild a filtered DataFrame copy per warrant and assign
    four derived columns to it — 2*W frame copies and 8*W column inserts per
    scan. Profiling a 338x59 chain put ~90% of the runtime in pandas
    (`Series.__init__`, `DataFrame.__setitem__`, `iterrows`) and none of it in
    the IV solve, so the candidate side moves to arrays. Same values, same
    order: the index arrays are ascending, so a type slice keeps frame order.
    """
    return {
        "n": len(opt_df),
        "type": opt_df["type"].to_numpy(),
        "strike": opt_df["strike"].to_numpy(dtype=float),
        "dte": opt_df["days_to_expiry"].to_numpy(dtype=np.int64),
        "contract": opt_df["contract"].tolist(),
        "ask_live": _flag(opt_df, "ask_live"),
        "bid_live": _flag(opt_df, "bid_live"),
        "bid": opt_df["bid"].to_numpy(dtype=float),
        "ask": opt_df["ask"].to_numpy(dtype=float),
        "iv": opt_df["_arb_opt_iv"].tolist(),
    }


def _pcp_option_arrays(opt_df):
    """Option columns as arrays for the PCP matcher, plus per-type row indices.

    Same reason as `_direct_option_arrays`: the per-warrant DataFrame copy and
    its two derived columns were the matcher's dominant cost.
    """
    n = len(opt_df)
    idx = np.arange(n)
    types = opt_df["type"].to_numpy()
    return {
        "n": n,
        "idx_by_type": {t: idx[types == t] for t in np.unique(types)},
        "strike": opt_df["strike"].to_numpy(dtype=float),
        "dte": opt_df["days_to_expiry"].to_numpy(dtype=np.int64),
        "contract": opt_df["contract"].tolist(),
        "underlying_price": opt_df["underlying_price"].to_numpy(dtype=float),
        "bond_pv": opt_df["_arb_bond_pv"].tolist(),
        "bid_ps": opt_df["_arb_bid_ps"].tolist(),
        "ask_ps": opt_df["_arb_ask_ps"].tolist(),
    }


def _leg_arrays(df, cols):
    """Named columns as plain lists/arrays, with the row indices of each type.

    Same motivation as `_direct_option_arrays`: it replaces a filtered DataFrame
    copy plus two derived columns built once per row of the other leg.
    """
    n = len(df)
    types = df["type"].to_numpy()
    out = {
        "n": n,
        "idx_by_type": {t: np.flatnonzero(types == t) for t in np.unique(types)},
        "strike_f": df["strike"].to_numpy(dtype=float),
        "dte_i": df["days_to_expiry"].to_numpy(dtype=np.int64),
    }
    for c in cols:
        out[c] = df[c].tolist() if c in df.columns else [None] * n
    return out


def _warrant_arrays(warrant_df):
    """Warrant columns as plain Python lists.

    `iterrows()` builds a Series per row and every `w["col"]` after it is an
    index lookup — ~50 per warrant per direction, which profiling showed was the
    remaining pandas cost once the candidate side moved to arrays. Lists give
    the same Python scalars the Series lookups did.
    """
    cols = ("warrant_code", "warrant_name", "type", "strike", "days_to_expiry",
            "exercise_ratio", "ask", "bid", "underlying_price", "ask_qty",
            "bid_qty", "volume", "iv_ask", "iv_bid", "_arb_warrant_iv")
    out = {"n": len(warrant_df)}
    for c in cols:
        out[c] = (warrant_df[c].tolist() if c in warrant_df.columns
                  else [None] * len(warrant_df))
    return out


def group_by_str(df, col):
    """`{str(value): sub-frame}` for one column, built once per scan.

    The orchestrators sliced with `df[col].astype(str) == str(code)` inside the
    per-code loop, re-casting the whole column for every code. groupby preserves
    within-group row order, so the slices are identical.
    """
    if df.empty or col not in df.columns:
        return {}
    return {str(k): g for k, g in df.groupby(df[col].astype(str), sort=False)}


def _quote_mid_iv(df, r):
    """Display IV off the two-sided mid, per row, for a whole frame.

    Both legs of the TW/US matcher price their IV off their own bid/ask mid, so
    neither depends on the pair it lands in — they were being solved inside the
    per-row loop.
    """
    n = len(df)
    if n == 0:
        return []
    bid = df["bid"].to_numpy(dtype=float)
    ask = df["ask"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        ok = (bid > 0) & (ask > 0)
        mid = np.where(ok, (bid + ask) / 2, np.nan)
    return _iv_batch(mid, df["underlying_price"].to_numpy(dtype=float),
                     df["strike"].to_numpy(dtype=float),
                     df["days_to_expiry"].to_numpy(), r, 1.0,
                     df["type"].to_numpy() == "Put")


def _with_pcp_option_cols(opt_df, r):
    """Attach the option-local values the PCP loop rebuilt once per warrant.

    Both per-share quotes and the bond leg's present value depend only on the
    option row and the discount rate, so W warrants meant W identical `np.exp`
    calls per option.
    """
    if opt_df.empty:
        return opt_df
    bid_ps, ask_ps = _live_per_share(opt_df)
    K = opt_df["strike"].to_numpy(dtype=float)
    dte = opt_df["days_to_expiry"].to_numpy()
    out = _object_col(opt_df, "_arb_bid_ps", bid_ps)
    out = _object_col(out, "_arb_ask_ps", ask_ps)
    out["_arb_bond_pv"] = [
        _round4(float(K[i]) * float(np.exp(-r * (int(dte[i]) / 365.0))))
        for i in range(len(opt_df))
    ]
    return out


def _pcp_opt_iv(opt_df, S, r):
    """PCP option-leg display IV for a whole frame at one spot.

    Unlike the same-type matcher there is no bid-only fallback: a book with no
    live ask has no mark, so the IV stays None.
    """
    mid = []
    for b, a in zip(opt_df["_arb_bid_ps"], opt_df["_arb_ask_ps"]):
        if b is not None and a is not None:
            mid.append((b + a) / 2)
        elif a is not None:
            mid.append(a)
        else:
            mid.append(None)
    return _opt_iv_batch(opt_df, S, r, opt_df["type"].to_numpy() == "Put", mid)


def _match_warrants_to_options(warrant_df, opt_df, opt_contract_size,
                               max_strike_diff_pct, max_dte_diff,
                               positive_loose=False):
    rows = []
    seen = set()  # deduplicate (warrant_code, option_contract) pairs

    # Both display IVs are properties of a single leg, so they are solved once
    # for the whole scan here rather than per (direction, warrant, option) —
    # see _with_warrant_iv / _with_direct_opt_iv. The warrant leg keeps its
    # hardcoded r=0.02 while the option leg uses options_logic.R; that
    # inconsistency is pinned by tests, not an oversight to tidy up here.
    warrant_df = _with_warrant_iv(warrant_df, 0.02)
    opt_df = _with_direct_opt_iv(opt_df)
    O = _direct_option_arrays(opt_df)
    W = _warrant_arrays(warrant_df)
    if O["n"] == 0 or W["n"] == 0:
        return rows

    # The candidate scan and both price branches run in the arb kernel — Rust
    # when built, logic/arb_kernels_py.py otherwise. Hits come back in the order
    # the per-row code emitted them (direction, then warrant, then option), so
    # the dedup and the row build stay here, where the field names, the None
    # conventions and the rounding are diffable against the original.
    # max_strike_diff_pct is kept in the signature only because callers pass it
    # positionally and _match_warrants_pcp still uses it.
    codes = {t: i for i, t in
             enumerate(sorted(set(O["type"].tolist()) | set(W["type"])))}
    hits = direct_pairs(
        [codes[t] for t in W["type"]],
        [t == "Put" for t in W["type"]],
        [float(x) for x in W["strike"]],
        [int(x) for x in W["days_to_expiry"]],
        [float(x) for x in W["exercise_ratio"]],
        [float(x) for x in W["ask"]],
        [float(x) if pd.notna(x) else float("nan") for x in W["bid"]],
        [codes[t] for t in O["type"].tolist()],
        # .tolist(), not the arrays: `round(np.float64, n)` rounds like NumPy
        # (scale-rint-unscale) while `round(float, n)` rounds decimally, and the
        # two disagree on ties — which reaches price_diff_pct.
        O["strike"].tolist(), O["dte"].tolist(), O["bid"].tolist(),
        O["ask"].tolist(), O["bid_live"].tolist(), O["ask_live"].tolist(),
        max_dte_diff, positive_loose,
    )

    for (wi, oi, dir_code, price_diff, exec_opt, exec_warrant,
         strike_diff_pct, dte_diff, favorable, max_loss_per_share) in hits:
        pair_key = (W["warrant_code"][wi], O["contract"][oi])
        if pair_key in seen:
            continue
        seen.add(pair_key)

        positive = dir_code == 0
        ratio = float(W["exercise_ratio"][wi])
        w_ask = W["ask"][wi]
        w_bid = W["bid"][wi]
        warrant_bid_live = pd.notna(w_bid) and float(w_bid or 0) > 0
        warrant_bid_disp = round(float(w_bid), 4) if warrant_bid_live else None
        warrants_needed = round(opt_contract_size / ratio)
        opt_ask_live = bool(O["ask_live"][oi])
        opt_bid_live = bool(O["bid_live"][oi])
        opt_bid_per_share = _round4(O["bid"][oi]) if opt_bid_live else None
        opt_ask_per_share = _round4(O["ask"][oi]) if opt_ask_live else None

        price_diff_pct = round(price_diff / exec_opt * 100, 2) if exec_opt > 0 else None

        if positive:
            trade = "Buy Warrant / Sell Option"
            # Buying the warrant lifts the ask -> resting SELL size gates it.
            warrant_depth_lots = int(W["ask_qty"][wi] or 0)
        else:
            trade = "Buy Option / Sell Warrant"
            # Selling the warrant hits the bid -> resting BUY size gates it.
            warrant_depth_lots = int(W["bid_qty"][wi] or 0)
        board_lots_needed = round(warrants_needed / 1000, 4)
        fillable = warrant_depth_lots >= board_lots_needed

        # IV per leg so the modal can mark an OTM surviving leg at its time
        # value ("sell") — the fetch dropped IV. Both solved once per leg above,
        # off the LIVE quote side(s) only: a settlement-backfilled side would
        # put a stale price into the solve.
        warrant_iv = W["_arb_warrant_iv"][wi]
        opt_iv = O["iv"][oi]

        rows.append({
            "warrant_code": W["warrant_code"][wi],
            "warrant_name": W["warrant_name"][wi],
            "option_contract": O["contract"][oi],
            "type": W["type"][wi],
            "trade": trade,
            "underlying_price": W["underlying_price"][wi],
            "warrant_dte": int(W["days_to_expiry"][wi]),
            "opt_dte": int(O["dte"][oi]),
            "dte_diff": int(dte_diff),
            "warrant_strike": W["strike"][wi],
            "opt_strike": round(float(O["strike"][oi]), 2),
            "strike_diff_pct": round(float(strike_diff_pct), 2),
            # Favorable residual = the vertical pays >= 0 at every spot, so
            # the entry credit is never clawed back: a true (riskless) arb.
            # Otherwise the credit is at risk up to max_loss_per_share
            # (per underlying share, before exercise-ratio sizing).
            "riskless": bool(favorable),
            "max_loss_per_share": round(float(max_loss_per_share), 4),
            "warrants_needed": warrants_needed,
            "board_lots": board_lots_needed,
            "warrant_depth_lots": warrant_depth_lots,
            "fillable": bool(fillable),
            "opt_contract_size": opt_contract_size,
            "warrant_ask": w_ask,
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

    # Everything that depends on one leg only is solved once here. The option
    # leg's IV also needs a spot: in the cross-market case that is the option's
    # own, so it resolves to a single column; otherwise it is the warrant's
    # local spot, which is shared by every warrant on an underlying — hence the
    # memo rather than a per-warrant solve.
    opt_df = opt_df.reset_index(drop=True)
    opt_df = _with_pcp_option_cols(opt_df, r)
    warrant_df = _with_warrant_iv(warrant_df, r)
    _opt_iv_all = (_pcp_opt_iv(opt_df, opt_df["underlying_price"].to_numpy(dtype=float), r)
                   if s_from_option and not opt_df.empty else None)
    _opt_iv_memo = {}

    def _opt_iv_at(spot):
        key = float(spot)
        if key not in _opt_iv_memo:
            _opt_iv_memo[key] = _pcp_opt_iv(opt_df, key, r)
        return _opt_iv_memo[key]

    P = _pcp_option_arrays(opt_df)
    W = _warrant_arrays(warrant_df)
    for wi in range(W["n"]):
        w_type = W["type"][wi]
        opp = "Put" if w_type == "Call" else "Call"
        cand = P["idx_by_type"].get(opp)
        if cand is None or cand.size == 0:
            continue

        ratio = float(W["exercise_ratio"][wi])
        if ratio <= 0:
            continue  # can't size or normalise a warrant with no exercise ratio

        Kw = float(W["strike"][wi])
        dte_w = int(W["days_to_expiry"][wi])
        Ko_all = P["strike"][cand]
        strike_diff_pct = np.abs(Ko_all - Kw) / Kw * 100
        dte_diff = np.abs(P["dte"][cand] - dte_w)
        sel = np.flatnonzero(strike_diff_pct <= max_strike_diff_pct)
        if sel.size == 0:
            continue

        S_local = float(W["underlying_price"][wi])   # warrant's own (local) spot
        is_call = w_type == "Call"
        w_ask = W["ask"][wi]
        w_bid = W["bid"][wi]
        warrant_bid_live = pd.notna(w_bid) and float(w_bid or 0) > 0
        warrant_ask_per_share = round(float(w_ask) / ratio, 4)
        warrant_bid_val = float(w_bid) if warrant_bid_live else float(w_ask)
        warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
        warrants_needed = round(opt_contract_size / ratio)
        warrant_bid_disp = round(float(w_bid), 4) if warrant_bid_live else None

        # Warrant-leg IV (display only; payoff is intrinsic), solved once above.
        warrant_iv = W["_arb_warrant_iv"][wi]
        opt_iv_col = _opt_iv_all if s_from_option else _opt_iv_at(S_local)

        for k in sel:
            oi = cand[k]
            # Spot for the synthetic: the option's underlying in the cross-market
            # case (ADR-converted), else the warrant's own local spot.
            S = float(P["underlying_price"][oi]) if s_from_option else S_local
            Ko = float(Ko_all[k])
            opt_dte = int(P["dte"][oi])
            bond_pv = P["bond_pv"][oi]
            # Gate on per-side liveness flagged BEFORE the settlement/last
            # fallback: opt["bid"]/["ask"] are post-fallback, so a missing side
            # carries a stale settlement mark that is not an executable price.
            opt_bid_ps = P["bid_ps"][oi]
            opt_ask_ps = P["ask_ps"][oi]
            # Option-leg IV (display only) — solved above off the OPTION's own
            # put/call flag, which is what `opp` selected these candidates on.
            opt_iv = opt_iv_col[oi]

            def _synth(opt_ps):
                # synthetic call = P + S - PV(K);  synthetic put = C - S + PV(K)
                return round((opt_ps + S - bond_pv) if is_call else (opt_ps - S + bond_pv), 4)

            def _emit(executable, opt_ps, warrant_ps, price_diff):
                synthetic_price = _synth(opt_ps)
                pct = round(price_diff / synthetic_price * 100, 2) if synthetic_price > 0 else None
                # Executable side buys the warrant (lifts ask -> SELL size gates);
                # the non-executable debug side would short it (hits bid).
                warrant_depth_lots = int((W["ask_qty"] if executable else W["bid_qty"])[wi] or 0)
                board_lots_needed = round(warrants_needed / 1000, 4)
                fillable = warrant_depth_lots >= board_lots_needed
                if executable:
                    if is_call:
                        trade = "Buy Call Warrant / Short Synthetic (short Put + short stock + lend)"
                    else:
                        trade = "Buy Put Warrant / Short Synthetic (short Call + long stock + borrow)"
                else:
                    side = "short Put + short stock + lend" if is_call else "short Call + long stock + borrow"
                    trade = f"Short {w_type} Warrant / Long Synthetic ({side}) — NON-EXECUTABLE"
                rows.append({
                    "warrant_code": W["warrant_code"][wi],
                    "warrant_name": W["warrant_name"][wi],
                    "option_contract": P["contract"][oi],
                    "type": w_type,
                    "opt_type": opp,
                    "trade": trade,
                    "executable": bool(executable),
                    "underlying_price": round(S_local, 4),
                    "adr_underlying": round(S, 4),
                    "warrant_dte": dte_w,
                    "opt_dte": opt_dte,
                    "dte_diff": int(dte_diff[k]),
                    "warrant_strike": round(Kw, 2),
                    "opt_strike": round(Ko, 2),
                    "strike_diff_pct": round(float(strike_diff_pct[k]), 2),
                    "warrants_needed": warrants_needed,
                    "board_lots": board_lots_needed,
                    "warrant_depth_lots": warrant_depth_lots,
                    "fillable": bool(fillable),
                    "opt_contract_size": opt_contract_size,
                    "warrant_ask": round(float(w_ask), 4),
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
                dte_ok = opt_dte <= dte_w
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
        WA = _warrant_arrays(warr)
        for wi in range(WA["n"]):
            ratio = float(WA["exercise_ratio"][wi] or 0)
            w_ask, w_bid = WA["ask"][wi], WA["bid"][wi]
            ask = float(w_ask or 0) if pd.notna(w_ask) else 0.0
            bid = float(w_bid or 0) if pd.notna(w_bid) else 0.0
            if ratio <= 0 or ask <= 0:
                continue
            if positive_loose and bid <= 0:
                continue
            buy_raw = bid if positive_loose else ask
            buy_ps = round(buy_raw / ratio, 6)
            K = round(float(WA["strike"][wi]), 4)
            cur = wings.get(K)
            if cur is None or buy_ps < cur["buy_ps"]:
                wings[K] = {
                    "K": K, "buy_ps": buy_ps, "ratio": ratio,
                    "ask": ask, "bid": bid if bid > 0 else None,
                    "dte": int(WA["days_to_expiry"][wi]),
                    "code": str(WA["warrant_code"][wi]), "name": WA["warrant_name"][wi],
                    "depth_lots": int(WA["ask_qty"][wi] or 0),
                    "S": float(WA["underlying_price"][wi]),
                }

        # Sellable body options: sell side = bid (tight) / ask (loose).
        bodies = []
        o_bid = opts["bid"].tolist()
        o_ask = opts["ask"].tolist()
        o_K = opts["strike"].tolist()
        o_dte = opts["days_to_expiry"].tolist()
        o_contract = opts["contract"].tolist()
        o_vol = (opts["volume"].tolist() if "volume" in opts.columns
                 else [0] * len(opts))
        o_S = opts["underlying_price"].tolist()
        for oi in range(len(opts)):
            bid = float(o_bid[oi] or 0) if pd.notna(o_bid[oi]) else 0.0
            ask = float(o_ask[oi] or 0) if pd.notna(o_ask[oi]) else 0.0
            sell_raw = ask if positive_loose else bid
            if sell_raw <= 0:
                continue
            bodies.append({
                "K": round(float(o_K[oi]), 4), "sell_ps": round(sell_raw, 6),
                "bid": bid if bid > 0 else None, "ask": ask if ask > 0 else None,
                "dte": int(o_dte[oi]),
                "contract": str(o_contract[oi]),
                "vol": int(o_vol[oi] or 0),
                "S": float(o_S[oi]),
            })
        if len(wings) < 2 or not bodies:
            continue

        # The wing-pair scan is O(K^2) over distinct warrant strikes and is the
        # matcher's whole remaining cost, so it runs in the arb kernel (Rust
        # when built, logic/arb_kernels_py.py otherwise). Bodies go in sorted by
        # strike with their chain positions, so a pair scans only the strikes
        # between the wings and a tie on sell price still keeps the body that
        # came first in the chain.
        by_k = sorted(range(len(bodies)), key=lambda i: bodies[i]["K"])
        strikes = sorted(wings)
        hits = butterfly_pairs(
            [wings[k]["K"] for k in strikes],
            [wings[k]["buy_ps"] for k in strikes],
            [wings[k]["dte"] for k in strikes],
            [bodies[i]["K"] for i in by_k],
            [bodies[i]["sell_ps"] for i in by_k],
            [bodies[i]["dte"] for i in by_k],
            by_k,
            is_call,
        )

        for a, b, bidx, credit_ps, tail, worst_payoff_ps, guaranteed_ps in hits:
                w1, w2 = wings[strikes[a]], wings[strikes[b]]
                K1, K2 = w1["K"], w2["K"]
                body = bodies[bidx]
                x = body["K"]

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

    # Fetch ONCE for every selected code, not per-code — read_warrant/read_tw_option
    # accept a list and the snapshot read already filters server-side on `codes`,
    # so a per-code loop would pay a redundant round-trip per stock. No time-value
    # cap / IV solve here: a positive price arb only needs warrant ask + option bid.
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

    warrant_groups = group_by_str(all_warrant_df, "underlying_code")
    opt_groups = group_by_str(all_opt_df, "stock_code")

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

        warrant_df = warrant_groups.get(str(code), all_warrant_df.iloc[0:0])
        if warrant_df.empty:
            msg = f"{code}: {warrant_err or 'no warrants'}"
            applog.log("ARB", f"{code} {pos} skipped: {warrant_err or 'no warrants'}",
                       level="ERROR" if warrant_hard else "INFO")
            return ([], None, msg) if warrant_hard else ([], msg, None)

        opt_df = opt_groups.get(str(code), all_opt_df.iloc[0:0])
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
        _finish_empty_scan(hard_errors, skip_reasons)

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
        _finish_empty_scan(hard_errors, skip_reasons)

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
    # Both display IVs price off their own leg's mid, so they are solved once
    # per frame rather than once per matched pair.
    us_df = us_df.reset_index(drop=True)
    tw_ivs = _quote_mid_iv(tw_df, options_logic.R)
    us_ivs = _quote_mid_iv(us_df, us_options_logic.R_US)

    T = _leg_arrays(tw_df, ("contract", "type", "underlying_price", "bid", "ask",
                            "ask_size", "bid_size"))
    U = _leg_arrays(us_df, ("contract", "type", "bid", "ask", "volume", "oi"))

    for ti in range(T["n"]):
        tw_type = T["type"][ti]
        cand = U["idx_by_type"].get(tw_type)
        if cand is None or cand.size == 0:
            continue

        tw_K = float(T["strike_f"][ti])
        tw_dte = int(T["dte_i"][ti])
        strike_diff_pct = np.abs(U["strike_f"][cand] - tw_K) / tw_K * 100
        dte_diff = np.abs(U["dte_i"][cand] - tw_dte)
        sel = np.flatnonzero((strike_diff_pct <= max_strike_diff_pct)
                             & (dte_diff <= max_dte_diff))
        if sel.size == 0:
            continue

        # Best pair = closest strike, then closest expiry (delta-match priority).
        # lexsort is the argmin of those two keys and breaks ties by original
        # order, exactly as pandas' multi-key (stable) sort did — without
        # sorting a candidate frame once per TW row.
        k = sel[np.lexsort((dte_diff[sel], strike_diff_pct[sel]))[0]]
        ui = cand[k]

        tw_b, tw_a = T["bid"][ti], T["ask"][ti]
        us_b, us_a = U["bid"][ui], U["ask"][ui]
        tw_bid = float(tw_b) if pd.notna(tw_b) and float(tw_b or 0) > 0 else None
        tw_ask = float(tw_a) if pd.notna(tw_a) and float(tw_a or 0) > 0 else None
        us_bid = float(us_b) if pd.notna(us_b) and float(us_b or 0) > 0 else None
        us_ask = float(us_a) if pd.notna(us_a) and float(us_a or 0) > 0 else None
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
        us_dte = int(U["dte_i"][ui])
        short_dte = us_dte if trade == "Long TW / Short US" else tw_dte
        long_dte = tw_dte if trade == "Long TW / Short US" else us_dte
        if short_dte > long_dte:
            continue  # would leave a naked short leg after the long expires

        # Strike must be on the FAVORABLE side or the pair is just a vertical
        # spread with a real max loss, not a no-downside structure. With a
        # received credit the no-loss vertical requires:
        #   Call: short strike >= long strike (short the higher call)
        #   Put:  short strike <= long strike (short the lower put)
        # Anything else has min payoff = credit − (unfavorable gap) < 0.
        us_K = float(U["strike_f"][ui])
        short_strike = us_K if trade == "Long TW / Short US" else tw_K
        long_strike = tw_K if trade == "Long TW / Short US" else us_K
        if tw_type == "Put":
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
        tw_iv = tw_ivs[ti]
        us_iv = us_ivs[ui]

        # Orderbook depth per leg. TW leg: best-level size (口) from TAIFEX MIS on
        # the side actually hit (buy TW@ask -> ask_size; sell TW@bid -> bid_size).
        # US leg: yfinance exposes no bid/ask size, so volume + OI stand in as a
        # liquidity proxy (NOT true resting depth).
        tw_size = (T["ask_size"] if trade == "Long TW / Short US" else T["bid_size"])[ti]
        tw_size = int(tw_size) if pd.notna(tw_size) else None
        tw_fillable = tw_size is not None and tw_size >= int(tw_contracts)
        us_vol = int(U["volume"][ui] or 0)
        us_oi = int(U["oi"][ui] or 0)

        denom = exec_opt if exec_opt else 1
        rows.append({
            "tw_option_code": T["contract"][ti],
            "tw_option_name": f"TW {T['contract'][ti]}",
            "us_option_contract": U["contract"][ui],
            "type": tw_type,
            "tw_option_type": tw_type,
            "us_option_type": U["type"][ui],
            "trade": trade,
            "underlying_price": round(float(T["underlying_price"][ti]), 4),
            "tw_option_dte": tw_dte,
            "us_option_dte": int(U["dte_i"][ui]),
            "dte_diff": int(dte_diff[k]),
            "tw_option_strike": round(tw_K, 2),
            "us_option_strike": round(float(U["strike_f"][ui]), 2),
            "strike_diff_pct": round(float(strike_diff_pct[k]), 2),
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
        _finish_empty_scan(hard_errors, skip_reasons)

    result = pd.DataFrame(all_rows)
    if "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result
