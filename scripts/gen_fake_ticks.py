"""Synthetic TSMC tick logs in exactly services/live_tick_log.py's CSV format,
for sanity-checking the end-of-day arb replay (logic/eod_arb_replay.py):
a Black-Scholes-consistent chain with bid/ask spreads and richly priced
warrants (no arb at any tick), optionally with injected Direct Match and
LP-only arbs at known times. Used by tests/logic/test_eod_arb_replay.py and
by hand:

    python scripts/gen_fake_ticks.py --out-dir /tmp/fake            # writes both files
    python scripts/gen_fake_ticks.py --out-dir /tmp/fake --every 1   # denser day
"""
import argparse
import csv
import math
import random
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logic import bs_python  # noqa: E402

TW_TZ = ZoneInfo("Asia/Taipei")
COLUMNS = ["ts", "kind", "code", "name", "type", "strike", "exercise_ratio", "expiry", "dte",
           "bid", "ask", "bid_size", "ask_size", "src"]

TRADE_DATE = date(2026, 10, 7)
S0 = 1000.0
R = 0.01875
OPT_IV = 0.30
WARRANT_IV = 0.42   # warrants trade rich to options, as they do in practice

E1 = TRADE_DATE + timedelta(days=14)
E2 = TRADE_DATE + timedelta(days=42)

# Injected arbs: (label, start, end) in Taipei wall-clock time. End None = still open at the close.
DIRECT_WINDOWS = [("direct_a", dtime(10, 15, 3), dtime(10, 15, 45)),
                  ("direct_a_reopen", dtime(11, 2, 10), dtime(11, 2, 30)),
                  ("direct_b_close", dtime(13, 25, 0), None)]
LP_WINDOWS = [("lp_spread", dtime(12, 0, 0), dtime(12, 2, 0))]

DIRECT_A = ("030001", "CDAJ6C1000")    # warrant 030001 vs option call K1000 E1
DIRECT_B = ("030009", "CDAJ6P1000")    # warrant put 030009 vs option put K1000 E1
LP_CHEAP_CALL = "CDBK6C950"            # E2 950 call offered below the E2 1000 call's bid


def _opt_code(exp, is_call, k):
    return f"CD{'A' if exp == E1 else 'B'}{'J' if exp == E1 else 'K'}6{'C' if is_call else 'P'}{int(k)}"


def _chain():
    """Static terms for 12 options and 15 warrants (one with terms arriving late)."""
    options = []
    for exp in (E1, E2):
        for k in (950.0, 1000.0, 1050.0):
            for is_call in (True, False):
                code = _opt_code(exp, is_call, k)
                options.append({"kind": "option", "code": code,
                                "name": f"台積電{exp:%Y%m} {int(k)} {'買權' if is_call else '賣權'}",
                                "type": "Call" if is_call else "Put", "strike": k,
                                "expiry": exp, "ratio": None})
    specs = [  # code, call?, strike, ratio, maturity offset (days)
        ("030001", True, 1000.0, 0.5, 40), ("030002", True, 950.0, 0.2, 60),
        ("030003", True, 1050.0, 0.5, 30), ("030004", True, 1000.0, 0.1, 90),
        ("030005", True, 900.0, 0.2, 120), ("030006", True, 1100.0, 0.5, 75),
        ("030007", True, 1000.0, 0.2, 20), ("030008", True, 980.0, 0.5, 50),
        ("030009", False, 1000.0, 0.5, 45), ("030010", False, 950.0, 0.5, 30),
        ("030011", False, 900.0, 0.2, 80), ("030012", False, 1000.0, 0.1, 100),
        ("030013", False, 970.0, 0.5, 25), ("030014", True, 1020.0, 0.5, 65),
        ("030015", False, 980.0, 0.2, 55),
    ]
    warrants = []
    for i, (code, is_call, k, ratio, off) in enumerate(specs):
        warrants.append({"kind": "warrant", "code": code,
                         "name": f"台積電元大{60 + i:02d}{'購' if is_call else '售'}{i + 1:02d}",
                         "type": "Call" if is_call else "Put", "strike": k,
                         "expiry": TRADE_DATE + timedelta(days=off), "ratio": ratio,
                         "late_terms": code == "030015"})
    return options, warrants


def _fair_ps(inst, spot):
    """Per-share fair value: BS at the instrument's IV, warrants floored at intrinsic."""
    t = max((inst["expiry"] - TRADE_DATE).days, 1) / 365.0
    is_call = inst["type"] == "Call"
    vol = WARRANT_IV if inst["kind"] == "warrant" else OPT_IV
    v = bs_python.bs_price(spot, inst["strike"], t, R, vol, 1.0, is_put=not is_call)
    if inst["kind"] == "warrant":
        v = max(v, max(0.0, spot - inst["strike"]) if is_call else max(0.0, inst["strike"] - spot))
    return v


def _quote(inst, spot, rng):
    """Best bid/ask/sizes around fair value, on the instrument's tick grid."""
    fair = _fair_ps(inst, spot)
    if inst["kind"] == "warrant":
        unit = fair * inst["ratio"]
        tick = 0.01 if unit < 10 else 0.05
        half = max(tick, unit * 0.01)
        bid = math.floor((unit - half) / tick) * tick
        ask = math.ceil((unit + half) / tick) * tick
        sizes = (rng.randint(20, 200), rng.randint(20, 200))
    else:
        tick = 0.5 if fair >= 10 else 0.1
        half = max(tick, fair * 0.015)
        bid = math.floor((fair - half) / tick) * tick
        ask = math.ceil((fair + half) / tick) * tick
        sizes = (rng.randint(1, 30), rng.randint(1, 30))
    bid = round(max(bid, 0.0), 2) or None
    return {"bid": bid, "ask": round(ask, 2), "bid_size": sizes[0] if bid else None, "ask_size": sizes[1]}


def _active(windows, label, t):
    for lab, start, end in windows:
        if lab == label and start <= t and (end is None or t < end):
            return True
    return False


def _apply_injections(inst, q, t, quotes, with_arb):
    """Override a quote while one of the injected arbs is live."""
    if not with_arb:
        return q
    q = dict(q)
    code = inst["code"]
    if code == DIRECT_A[0] and (_active(DIRECT_WINDOWS, "direct_a", t) or _active(DIRECT_WINDOWS, "direct_a_reopen", t)):
        # Offer the warrant 2 NT$/share under the option's bid — buy warrant, sell option.
        opt_bid = quotes[DIRECT_A[1]]["bid"]
        q["ask"] = round((opt_bid - 2.0) * inst["ratio"], 2)
        q["bid"] = round(q["ask"] - 0.05, 2)
        q["ask_size"] = 100
    if code == DIRECT_B[0] and _active(DIRECT_WINDOWS, "direct_b_close", t):
        opt_bid = quotes[DIRECT_B[1]]["bid"]
        q["ask"] = round((opt_bid - 3.0) * inst["ratio"], 2)
        q["bid"] = round(q["ask"] - 0.05, 2)
        q["ask_size"] = 100
    if code == LP_CHEAP_CALL and _active(LP_WINDOWS, "lp_spread", t):
        # A lower-strike call offered below a higher-strike call's bid: an option-only
        # vertical (buy 950C / sell 1000C) Direct Match cannot see.
        k1000_bid = quotes["CDBK6C1000"]["bid"]
        q["ask"] = round(k1000_bid - 3.0, 1)
        q["bid"] = round(q["ask"] - 1.0, 1)
        q["ask_size"] = 20
    return q


def _row(inst, ts, q, src, with_terms=True):
    terms = with_terms or not inst.get("late_terms")
    return {
        "ts": ts.isoformat(timespec="milliseconds"), "kind": inst["kind"], "code": inst["code"],
        "name": inst["name"],
        "type": inst["type"] if (terms or inst["kind"] == "warrant") else None,
        "strike": inst["strike"] if terms else None,
        "exercise_ratio": inst["ratio"] if (terms and inst["kind"] == "warrant") else None,
        "expiry": inst["expiry"].isoformat() if terms else None,
        "dte": (inst["expiry"] - TRADE_DATE).days if terms else None,
        "bid": q["bid"], "ask": q["ask"], "bid_size": q["bid_size"], "ask_size": q["ask_size"],
        "src": src,
    }


def generate(path, with_arb, every_s=2.0, seed=7, start=dtime(9, 0), end=dtime(13, 30)):
    """Write one synthetic trading day to `path`; returns the row count."""
    rng = random.Random(seed)
    options, warrants = _chain()
    insts = options + warrants
    by_code = {i["code"]: i for i in insts}
    t0 = datetime.combine(TRADE_DATE, start, TW_TZ)
    t_end = datetime.combine(TRADE_DATE, end, TW_TZ)
    spot = S0
    quotes = {}
    late = next(i for i in warrants if i.get("late_terms"))
    terms_at = t0 + timedelta(minutes=5)
    rows = []

    def emit(inst, ts, q, src):
        known = not inst.get("late_terms") or ts >= terms_at
        rows.append(_row(inst, ts, q, src, with_terms=known))

    # Recorder start: one snapshot row per tracked code (options first so warrant
    # injections can read option bids).
    for inst in insts:
        quotes[inst["code"]] = _apply_injections(inst, _quote(inst, spot, rng), start, quotes, with_arb)
        emit(inst, t0, quotes[inst["code"]], "snapshot")

    # Every injection boundary gets an explicit tick on the injected instrument,
    # so episode start/end times are exact; the late warrant's terms arrive as a
    # "terms" row (live_warrant.py logs one when its terms fetch lands).
    events = [(terms_at, late["code"], "terms")]
    for windows, codes in ((DIRECT_WINDOWS, {"direct_a": DIRECT_A[0], "direct_a_reopen": DIRECT_A[0],
                                             "direct_b_close": DIRECT_B[0]}),
                           (LP_WINDOWS, {"lp_spread": LP_CHEAP_CALL})):
        for lab, s_, e_ in windows:
            for bt in (s_, e_):
                if bt is not None:
                    events.append((datetime.combine(TRADE_DATE, bt, TW_TZ), codes[lab], "ws"))
    events.sort(key=lambda e: e[0])

    def requote(inst, ts, move_sizes_only=False):
        old = quotes[inst["code"]]
        if move_sizes_only:
            q = {**old, "bid_size": rng.randint(1, 200) if old["bid"] else None,
                 "ask_size": rng.randint(1, 200)}
        else:
            q = _quote(inst, spot, rng)
        q = _apply_injections(inst, q, ts.time(), quotes, with_arb)
        if q != old or move_sizes_only:
            quotes[inst["code"]] = q
            emit(inst, ts, q, "ws")

    # Market makers requote the whole chain when spot moves (one burst, one
    # timestamp — options first so warrant injections see fresh option bids);
    # between moves only resting sizes change. Requoting one stale instrument
    # at a time instead would leave cross-instrument quotes genuinely
    # arbitrageable, which is exactly what the no-arb day must not contain.
    t = t0
    step = timedelta(seconds=every_s)
    ei = 0
    while t < t_end:
        t = t + step
        while ei < len(events) and events[ei][0] <= t:
            ets, code, src = events[ei]
            ei += 1
            inst = by_code[code]
            q = _apply_injections(inst, _quote(inst, spot, rng), ets.time(), quotes, with_arb)
            quotes[code] = q
            emit(inst, ets, q, src)
        if rng.random() < 0.3:
            spot *= math.exp(rng.gauss(0, 0.0003 * math.sqrt(every_s / 2.0)))
            for inst in insts:
                requote(inst, t)
        else:
            requote(rng.choice(insts), t, move_sizes_only=True)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--every", type=float, default=2.0, help="seconds between background ticks")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    stamp = TRADE_DATE.strftime("%Y%m%d")
    out = Path(args.out_dir)
    for name, arb in ((f"fake_noarb_tsmc_ticks_{stamp}.csv", False), (f"fake_arb_tsmc_ticks_{stamp}.csv", True)):
        n = generate(out / name, arb, every_s=args.every, seed=args.seed)
        print(f"wrote {out / name} ({n:,} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
