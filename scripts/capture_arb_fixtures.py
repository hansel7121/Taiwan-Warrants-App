"""Record golden fixtures for logic/arb_logic.py's matchers.

The arb matchers have no numeric test — `tests/logic/test_arb_logic_no_matches.py`
only pins the NoMatchesError-vs-RuntimeError contract on empty frames. Nothing
can safely be refactored or ported to Rust until current behaviour is written
down, and hand-computing expected values for a 35-key row across a dozen branchy
scenarios is neither feasible nor trustworthy. So: record it.

Run ONCE on unmodified code, commit the output, and treat it as immutable
thereafter (see tests/fixtures/arb/README.md).

    python scripts/capture_arb_fixtures.py            # live frames (needs network)
    python scripts/capture_arb_fixtures.py --offline  # reuse the committed frames

`--offline` re-derives only `expected.json` from the frames already on disk,
which is how you confirm a refactor is a no-op without a market connection.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logic import arb_logic, iv_engine, options_logic, us_options_logic, warrant_logic  # noqa: E402
from tests import frame_codec as fc  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "arb"
# Input frames are shared: most scenarios differ only in parameters, so storing
# one copy per (base, mutation) keeps the corpus small and makes it obvious at a
# glance which scenarios are looking at the same chain.
FRAME_DIR = FIXTURES / "_frames"

TW_CODE = "2330"   # deepest warrant chain + a liquid TAIFEX option chain
US_CODE = "2303"   # UMC: the underlying that carries both TW options and a US ADR


# ── base frames, prepared exactly as the orchestrators prepare them ──────────

def base_tw(code=TW_CODE):
    """(warrant_df, opt_df) as match_warrant_tw_option hands them to a matcher."""
    w, _, _ = warrant_logic.read_warrant([code], "All", 0, 365, 0, 1e9, 0, compute_iv=False)
    o, _, _ = options_logic.read_tw_option([code], "All", min_days=1, compute_iv=False)
    if not o.empty:
        o = o[o["ask_live"] | o["bid_live"]]
    w = w[w["underlying_code"].astype(str) == str(code)] if not w.empty else w
    o = o[o["stock_code"].astype(str) == str(code)] if not o.empty else o
    return {"warrant_df": w, "opt_df": o}


def base_us(code=US_CODE):
    """(warrant_df, opt_df) as match_warrant_us_option hands them to a matcher."""
    w, _, _ = warrant_logic.read_warrant([code], "All", 0, 365, 0, 1e9, 0, compute_iv=False)
    o, _, _ = us_options_logic.read_us_option([code], "All", min_days=1, compute_iv=False)
    if not o.empty:
        o = o[o["ask_live"] | o["bid_live"]]
    return {"warrant_df": w, "opt_df": o}


def base_tw_us(code=US_CODE):
    """(tw_df, us_df) as match_tw_us_option hands them to _match_option_legs."""
    tw, _, _ = options_logic.read_tw_option([code], "All", min_days=1, compute_iv=False)
    us, _, _ = us_options_logic.read_us_option([code], "All", min_days=1, compute_iv=False)
    if not us.empty:
        us = us[us["is_live"]]
    return {"tw_df": tw, "us_df": us}


BASES = {"tw": base_tw, "us": base_us, "tw_us": base_tw_us}


# ── deterministic mutations that steer rows into specific branches ───────────

def _every(df, n, col, value):
    """Set `col` to `value` on every n-th row — positional, so it is reproducible."""
    out = df.copy()
    if out.empty:
        return out
    out.iloc[::n, out.columns.get_loc(col)] = value
    return out


def mut_none(f):
    return f


def mut_no_warrant_bid(f):
    """Warrants with no live bid exercise the `warrant_bid_real_per_share is None` skip."""
    return {**f, "warrant_df": _every(f["warrant_df"], 2, "bid", 0.0)}


def mut_option_ask_only(f):
    """Options quoting only an ask exercise the bid-side gate in every matcher."""
    o = _every(f["opt_df"], 2, "bid", np.nan)
    o = _every(o, 2, "bid_live", False)
    return {**f, "opt_df": o}


def mut_option_bid_only(f):
    o = _every(f["opt_df"], 3, "ask", np.nan)
    o = _every(o, 3, "ask_live", False)
    return {**f, "opt_df": o}


def mut_bad_ratio(f):
    """exercise_ratio <= 0 must drop the warrant, not divide by zero."""
    return {**f, "warrant_df": _every(f["warrant_df"], 5, "exercise_ratio", 0.0)}


def mut_iv_present(f):
    """Scanner frames arrive with iv_ask solved; the arb path normally gets NaN.

    Both branches matter: the IV batching only touches the NaN branch, so a
    fixture that only covers one of them proves half the refactor.
    """
    w = f["warrant_df"].copy()
    o = f["opt_df"].copy()
    if not w.empty:
        w["iv_ask"] = np.linspace(0.2, 0.8, len(w))
    if not o.empty:
        o["iv_ask"] = np.linspace(0.15, 0.6, len(o))
    return {**f, "warrant_df": w, "opt_df": o}


MUTATIONS = {
    "none": mut_none,
    "no_warrant_bid": mut_no_warrant_bid,
    "option_ask_only": mut_option_ask_only,
    "option_bid_only": mut_option_bid_only,
    "bad_ratio": mut_bad_ratio,
    "iv_present": mut_iv_present,
}


# ── matchers under test ─────────────────────────────────────────────────────

def run_same_type(f, p):
    return arb_logic._match_warrants_to_options(
        f["warrant_df"], f["opt_df"], p["opt_contract_size"],
        p["max_strike_diff_pct"], p["max_dte_diff"],
        positive_loose=p["positive_loose"],
    )


def run_pcp(f, p):
    return arb_logic._match_warrants_pcp(
        f["warrant_df"], f["opt_df"], p["opt_contract_size"],
        p["max_strike_diff_pct"], p["max_dte_diff"],
        positive_loose=p["positive_loose"], r=p.get("r"),
        synthetic_underlying=p.get("synthetic_underlying", "warrant"),
    )


def run_butterfly(f, p):
    return arb_logic._match_butterflies(
        f["warrant_df"], f["opt_df"], p["opt_contract_size"],
        positive_loose=p["positive_loose"],
    )


def run_option_legs(f, p):
    return arb_logic._match_option_legs(
        f["tw_df"], f["us_df"], p["tw_contract_shares"], p["us_contract_shares"],
        p["max_strike_diff_pct"], p["max_dte_diff"],
        positive_loose=p["positive_loose"],
    )


MATCHERS = {
    "same_type": run_same_type,
    "pcp": run_pcp,
    "butterfly": run_butterfly,
    "option_legs": run_option_legs,
}

R_TW = None  # _match_warrants_pcp defaults r to options_logic.R

SCENARIOS = [
    # name, base, mutation, matcher, params
    ("direct_strict",        "tw", "none",            "same_type", {"positive_loose": False}),
    ("direct_loose",         "tw", "none",            "same_type", {"positive_loose": True}),
    ("direct_no_warrant_bid","tw", "no_warrant_bid",  "same_type", {"positive_loose": False}),
    ("direct_ask_only",      "tw", "option_ask_only", "same_type", {"positive_loose": False}),
    ("direct_bid_only",      "tw", "option_bid_only", "same_type", {"positive_loose": True}),
    ("direct_bad_ratio",     "tw", "bad_ratio",       "same_type", {"positive_loose": False}),
    # The warrant leg's IV is solved with a hardcoded r=0.02 while the option
    # leg uses options_logic.R. This scenario is what breaks if someone unifies
    # them; see test_warrant_leg_iv_uses_hardcoded_r_002.
    ("direct_iv_present",    "tw", "iv_present",      "same_type", {"positive_loose": False}),
    ("pcp_warrant_strict",   "tw", "none",            "pcp", {"positive_loose": False}),
    ("pcp_warrant_loose",    "tw", "none",            "pcp", {"positive_loose": True}),
    ("pcp_warrant_ask_only", "tw", "option_ask_only", "pcp", {"positive_loose": False}),
    ("butterfly_strict",     "tw", "none",            "butterfly", {"positive_loose": False}),
    ("butterfly_loose",      "tw", "none",            "butterfly", {"positive_loose": True}),
    ("us_direct_strict",     "us", "none",            "same_type", {"positive_loose": False}),
    ("us_pcp_option",        "us", "none",            "pcp",
     {"positive_loose": False, "synthetic_underlying": "option"}),
    ("tw_us_legs",           "tw_us", "none",         "option_legs", {"positive_loose": False}),
]

DEFAULTS = {
    "max_strike_diff_pct": 3.0,
    "max_dte_diff": 5,
    "positive_loose": False,
}


def _params(base, extra):
    p = dict(DEFAULTS)
    if base == "tw":
        p["opt_contract_size"] = options_logic._commodity_map()[TW_CODE]["exercise_ratio"]
    elif base == "us":
        p["opt_contract_size"] = us_options_logic.contract_tw_shares(US_CODE)
    else:
        p["tw_contract_shares"] = options_logic._commodity_map()[US_CODE]["exercise_ratio"]
        p["us_contract_shares"] = us_options_logic.contract_tw_shares(US_CODE)
    if extra.get("synthetic_underlying") == "option":
        p["r"] = us_options_logic.R_US
    p.update(extra)
    return p


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True).strip()
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="reuse the committed frames; only re-derive expected.json")
    ap.add_argument("--only", help="capture just this scenario")
    args = ap.parse_args()

    bases = {}
    if not args.offline:
        for name, fn in BASES.items():
            bases[name] = fn()
            sizes = ", ".join(f"{k}={len(v)}" for k, v in bases[name].items())
            print(f"base {name}: {sizes}")

    sha = _git_sha()
    for name, base, mutation, matcher, extra in SCENARIOS:
        if args.only and args.only != name:
            continue
        d = FIXTURES / name
        d.mkdir(parents=True, exist_ok=True)
        FRAME_DIR.mkdir(parents=True, exist_ok=True)
        frames_id = f"{base}__{mutation}"
        frames_path = FRAME_DIR / f"{frames_id}.json"

        if args.offline:
            frames = {k: fc.load_frame(v) for k, v in fc.read_json(frames_path).items()}
        else:
            frames = MUTATIONS[mutation](bases[base])
            fc.write_json(frames_path, {k: fc.dump_frame(v) for k, v in frames.items()})

        p = _params(base, extra)
        p["frames"] = frames_id
        rows = MATCHERS[matcher](frames, p)
        fc.write_json(d / "params.json", {"matcher": matcher, **p})
        fc.write_json(d / "expected.json", fc.dump_rows(rows))
        if not args.offline:
            fc.write_json(d / "provenance.json", {
                "git_sha": sha,
                "engine": iv_engine.engine_info(),
                "source": "live",
                "base": base,
                "mutation": mutation,
                "input_sizes": {k: len(v) for k, v in frames.items()},
            })
        sizes = " ".join(f"{k}={len(v)}" for k, v in frames.items())
        print(f"{name:<24} {matcher:<15} {sizes:<28} rows={len(rows)}")


if __name__ == "__main__":
    main()
