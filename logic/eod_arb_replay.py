"""End-of-day replay of a recorded TSMC tick log (services/live_tick_log.py's
CSV): folds every tick into per-code books, re-runs Direct Match and the
static-arb LP on the Python implementations after each change, and turns the
arb sets over time into episodes (when an arb appeared, when it ended, how
long it lasted, its peak edge). Driven by scripts/eod_arb_replay.py, which the
scheduler launches after the close; pure — no Supabase, no Flask, no clock.
"""
import csv
import gzip
import time
from datetime import date, datetime, time as dtime

from logic import arb_kernels_py, live_arb_logic, live_arb_lp_logic, lp_screen

SESSION_OPEN = dtime(9, 0)
SESSION_CLOSE = dtime(13, 30)

_NUM = ("strike", "exercise_ratio", "dte", "bid", "ask", "bid_size", "ask_size")


# ---------------------------------------------------------------------------
# Reading the tick log
# ---------------------------------------------------------------------------
def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if f != f else f


def parse_row(raw):
    """One CSV row (strings) -> typed tick dict; None for an unusable row."""
    try:
        ts = datetime.fromisoformat(raw["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    kind, code = raw.get("kind"), raw.get("code")
    if kind not in ("warrant", "option") or not code:
        return None
    tick = {"ts": ts, "kind": kind, "code": code, "name": raw.get("name") or code,
            "type": raw.get("type") or None, "src": raw.get("src") or None}
    for k in _NUM:
        tick[k] = _num(raw.get(k))
    try:
        tick["expiry"] = date.fromisoformat(raw["expiry"]) if raw.get("expiry") else None
    except ValueError:
        tick["expiry"] = None
    return tick


def iter_ticks(path):
    """Typed ticks from a .csv or .csv.gz tick log, in file order."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            tick = parse_row(raw)
            if tick is not None:
                yield tick


def _book_row(tick):
    """The snapshot-row shape live_arb_logic / live_arb_lp_logic read."""
    row = {
        "code": tick["code"], "name": tick["name"], "type": tick["type"],
        "strike": tick["strike"],
        "best": {"bid": tick["bid"], "ask": tick["ask"],
                 "bid_size": tick["bid_size"], "ask_size": tick["ask_size"]},
    }
    if tick["kind"] == "warrant":
        row["exercise_ratio"] = tick["exercise_ratio"]
        row["maturity"] = tick["expiry"]
    else:
        row["expiry"] = tick["expiry"]
    return row


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------
class EpisodeTracker:
    """Turns a sequence of "currently active arbs" snapshots into episodes."""

    def __init__(self, peak_field, codes_fn=None):
        self.peak_field = peak_field
        self.codes_fn = codes_fn   # row -> instrument codes, accumulated per episode
        self.active = {}   # key -> open episode
        self.closed = []

    def observe(self, ts, current):
        """`current` is {key: row} of every arb active right after the tick at `ts`."""
        for key in [k for k in self.active if k not in current]:
            self._close(key, ts, open_at_close=False)
        for key, row in current.items():
            ep = self.active.get(key)
            if ep is None:
                ep = self.active[key] = {"key": key, "started_at": ts, "open_row": row,
                                         "peak_row": row, "peak_at": ts, "codes": set()}
            elif (row.get(self.peak_field) or 0) > (ep["peak_row"].get(self.peak_field) or 0):
                ep["peak_row"], ep["peak_at"] = row, ts
            if self.codes_fn:
                ep["codes"].update(self.codes_fn(row))

    def _close(self, key, ts, open_at_close):
        ep = self.active.pop(key)
        ep["ended_at"] = ts
        ep["duration_s"] = round((ts - ep["started_at"]).total_seconds(), 3)
        ep["open_at_close"] = open_at_close
        self.closed.append(ep)

    def finish(self, ts):
        """Close everything still open at the last tick; returns all episodes in start order."""
        for key in list(self.active):
            self._close(key, ts, open_at_close=True)
        return sorted(self.closed, key=lambda e: (e["started_at"], e["key"]))


def direct_key(row):
    return f"{row['warrant_code']}:{row['option_code']}"


def lp_key(row):
    """An LP episode is "some riskless structure exists at this horizon": the
    solver's optimal leg set reshuffles free-rider legs tick to tick while the
    underlying mispricing persists, so keying on exact legs would shatter one
    arb into dozens of episodes. Legs at open/peak and every leg seen are kept."""
    return f"h{row['horizon_dte']}"


def lp_codes(row):
    return {l["code"] for l in row["legs"]}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def _lp_horizons_touched(kind, old, new, horizons):
    """Option-expiry horizons whose LP inputs this book change can move.

    A warrant enters only as a long (ask side) at horizons <= its maturity; an
    option is a short (bid side) only at its own expiry and a long (ask side)
    at horizons <= its expiry. A terms change counts as both sides moving.
    """
    exp_key = "maturity" if kind == "warrant" else "expiry"

    def terms(r):
        return (r["type"], r["strike"], r.get("exercise_ratio"), r.get(exp_key))

    def side(r, s):
        return r["best"][s], r["best"][f"{s}_size"]

    terms_moved = old is None or terms(old) != terms(new)
    ask_moved = terms_moved or side(old, "ask") != side(new, "ask")
    bid_moved = kind == "option" and (terms_moved or side(old, "bid") != side(new, "bid"))

    out = set()
    for r in [new] + ([old] if old is not None and terms_moved else []):
        exp = r.get(exp_key)
        if exp is None:
            continue
        if ask_moved:
            out.update(h for h in horizons if h <= exp)
        if bid_moved and exp in horizons:
            out.add(exp)
    return sorted(out)


def _in_session(ts):
    return SESSION_OPEN <= ts.time() <= SESSION_CLOSE


def _groups_by_ts(ticks, stats):
    """Yield (ts, [ticks]) for each run of ticks sharing a timestamp.

    The recorder buffers rows from several threads, so file order can be a few
    ms out of ts order; event time is clamped so it never runs backwards.
    """
    cur_ts, group = None, []
    for tick in ticks:
        stats["n_ticks"] += 1
        ts = tick["ts"] if cur_ts is None or tick["ts"] >= cur_ts else cur_ts
        if group and ts != cur_ts:
            yield cur_ts, group
            group = []
        cur_ts = ts
        group.append(tick)
    if group:
        yield cur_ts, group


def replay(ticks, trade_date, run_lp=True, lp_min_edge=0.0, progress=None, progress_every=5000,
           use_screen=True):
    """Replay `ticks` (iter_ticks output) for `trade_date`; returns
    {"direct": [episode], "lp": [episode], "stats": {...}}.

    Direct Match is rescanned incrementally — a tick on one warrant re-pairs
    only that warrant against every option (and vice versa) — which is exactly
    equivalent to a full rescan because direct_pairs is pairwise independent.
    The LP is re-checked only for the horizons whose inputs the tick moved,
    through a warm-started relaxation screen (logic/lp_screen.py); the full
    static_arb solver runs only when that relaxation could hold an arb
    (`use_screen=False` solves fully every time — same answer, for tests).
    Ticks sharing a timestamp are folded together and scanned once. Ticks
    outside 09:00–13:30 update the books but are not scanned.
    """
    t0 = time.perf_counter()
    books = {"warrant": {}, "option": {}}
    direct_hits = {}      # (warrant_code, option_code) -> row
    lp_by_horizon = {}    # expiry date -> row | None
    screens = {}          # expiry date -> lp_screen.HorizonScreen
    direct_tracker = EpisodeTracker("price_diff")
    lp_tracker = EpisodeTracker("guaranteed_profit", codes_fn=lp_codes)
    stats = {"n_ticks": 0, "n_changes": 0, "n_scans": 0, "n_lp_skips": 0, "n_lp_screens": 0, "n_lp_solves": 0,
             "first_ts": None, "last_ts": None}
    last_ts = None
    primed = False   # full scan done for the current session stretch

    def scan_direct(w_rows, o_rows):
        return live_arb_logic.scan(w_rows, o_rows, trade_date, pairs_fn=arb_kernels_py.direct_pairs)

    def build_screen(hz):
        longs, shorts = live_arb_lp_logic._build_legs(
            list(books["warrant"].values()), list(books["option"].values()), hz, trade_date)
        screens[hz] = lp_screen.HorizonScreen()
        screens[hz].build(longs, shorts)

    def check_lp(hz):
        """The horizon's LP row (or None), solving fully only when the screen says it might be an arb."""
        if use_screen:
            verdict = screens[hz].must_full_solve(lp_min_edge)
            if verdict is None:
                stats["n_lp_skips"] += 1
                return None
            stats["n_lp_screens"] += 1
            if not verdict:
                return None
        stats["n_lp_solves"] += 1
        return live_arb_lp_logic.scan_horizon(
            list(books["warrant"].values()), list(books["option"].values()),
            hz, trade_date, lp_min_edge, engine="python")

    def push_instrument(hz, kind, code, row):
        """Update one instrument's legs in the horizon's screen (rebuilding if its terms moved)."""
        w, o = ([row], []) if kind == "warrant" else ([], [row])
        longs, shorts = live_arb_lp_logic._build_legs(w, o, hz, trade_date)
        screens[hz].apply_instrument(code, longs, shorts)
        if screens[hz].needs_rebuild:
            build_screen(hz)

    def full_rescan():
        direct_hits.clear()
        for h in scan_direct(list(books["warrant"].values()), list(books["option"].values())):
            direct_hits[(h["warrant_code"], h["option_code"])] = h
        lp_by_horizon.clear()
        screens.clear()
        if run_lp:
            for hz in live_arb_lp_logic.horizons(list(books["option"].values())):
                build_screen(hz)
                lp_by_horizon[hz] = check_lp(hz)

    def observe(ts):
        direct_tracker.observe(ts, {f"{w}:{o}": r for (w, o), r in direct_hits.items()})
        lp_tracker.observe(ts, {lp_key(r): r for r in lp_by_horizon.values() if r is not None})

    for ts, group in _groups_by_ts(ticks, stats):
        last_ts = ts
        if stats["first_ts"] is None:
            stats["first_ts"] = ts

        # Fold every tick sharing this timestamp first, so a multi-code requote
        # is scanned once in its final state, never half-applied.
        before = {}
        for tick in group:
            kind, code = tick["kind"], tick["code"]
            new = _book_row(tick)
            old = books[kind].get(code)
            if old is not None and old == new:
                continue
            before.setdefault((kind, code), old)
            books[kind][code] = new
        changed = [(k, c, old, books[k][c]) for (k, c), old in before.items() if old != books[k][c]]
        if not changed:
            continue
        stats["n_changes"] += len(changed)

        if not _in_session(ts):
            continue   # books still fold; anything open at the bell closes in finish()

        stats["n_scans"] += 1
        if not primed:
            full_rescan()
            primed = True
        else:
            for kind, code, _, new in changed:
                if kind == "warrant":
                    for key in [k for k in direct_hits if k[0] == code]:
                        del direct_hits[key]
                    for h in scan_direct([new], list(books["option"].values())):
                        direct_hits[(h["warrant_code"], h["option_code"])] = h
                else:
                    for key in [k for k in direct_hits if k[1] == code]:
                        del direct_hits[key]
                    for h in scan_direct(list(books["warrant"].values()), [new]):
                        direct_hits[(h["warrant_code"], h["option_code"])] = h
            if run_lp:
                hz_all = live_arb_lp_logic.horizons(list(books["option"].values()))
                for gone in [h for h in lp_by_horizon if h not in hz_all]:
                    del lp_by_horizon[gone]
                    screens.pop(gone, None)
                todo = set()
                for hz in hz_all:
                    if hz not in screens:
                        build_screen(hz)
                        todo.add(hz)
                for kind, code, old, new in changed:
                    for hz in _lp_horizons_touched(kind, old, new, hz_all):
                        if hz not in todo:
                            push_instrument(hz, kind, code, new)
                        todo.add(hz)
                for hz in sorted(todo):
                    lp_by_horizon[hz] = check_lp(hz)
        observe(ts)

        if progress and stats["n_scans"] % progress_every == 0:
            progress(stats)

    end_ts = last_ts
    stats["last_ts"] = end_ts
    stats["runtime_s"] = round(time.perf_counter() - t0, 2)
    if end_ts is None:
        return {"direct": [], "lp": [], "stats": stats}
    close_ts = min(end_ts, datetime.combine(end_ts.date(), SESSION_CLOSE, end_ts.tzinfo))
    return {
        "direct": direct_tracker.finish(close_ts),
        "lp": lp_tracker.finish(close_ts),
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Rows for storage
# ---------------------------------------------------------------------------
def _iso(ts):
    return ts.isoformat(timespec="milliseconds")


def _episode_id(trade_date, key, started_at):
    return f"{trade_date.isoformat()}:{key}:{started_at.strftime('%H%M%S%f')[:9]}"


def direct_records(episodes, trade_date):
    """Direct Match episodes flattened for eod_arb_direct_episodes."""
    out = []
    for ep in episodes:
        r, p = ep["open_row"], ep["peak_row"]
        out.append({
            "id": _episode_id(trade_date, ep["key"], ep["started_at"]),
            "trade_date": trade_date.isoformat(),
            "warrant_code": r["warrant_code"], "warrant_name": r["warrant_name"],
            "option_code": r["option_code"], "option_name": r["option_name"],
            "type": r["type"],
            "warrant_strike": r["warrant_strike"], "opt_strike": r["opt_strike"],
            "warrant_dte": r["warrant_dte"], "opt_dte": r["opt_dte"],
            "started_at": _iso(ep["started_at"]), "ended_at": _iso(ep["ended_at"]),
            "duration_s": ep["duration_s"], "open_at_close": ep["open_at_close"],
            "warrant_ask": r["warrant_ask"], "warrant_ask_size": r["warrant_ask_size"],
            "opt_bid": r["opt_bid"], "opt_bid_size": r["opt_bid_size"],
            "price_diff": r["price_diff"], "price_diff_pct": r["price_diff_pct"],
            "riskless": r["riskless"],
            "peak_price_diff": p["price_diff"], "peak_price_diff_pct": p["price_diff_pct"],
            "peak_at": _iso(ep["peak_at"]),
        })
    return out


def lp_records(episodes, trade_date):
    """LP episodes flattened for eod_arb_lp_episodes."""
    out = []
    for ep in episodes:
        r, p = ep["open_row"], ep["peak_row"]
        out.append({
            "id": _episode_id(trade_date, ep["key"], ep["started_at"]),
            "trade_date": trade_date.isoformat(),
            "horizon_dte": r["horizon_dte"],
            "leg_codes": "|".join(sorted(ep["codes"])),
            "legs": r["legs"], "peak_legs": p["legs"],
            "started_at": _iso(ep["started_at"]), "ended_at": _iso(ep["ended_at"]),
            "duration_s": ep["duration_s"], "open_at_close": ep["open_at_close"],
            "net_credit": r["net_credit"], "guaranteed_profit": r["guaranteed_profit"],
            "min_payoff": r["min_payoff"], "worst_spot": r["worst_spot"],
            "gross_debit": r["gross_debit"], "return_pct": r["return_pct"],
            "peak_guaranteed_profit": p["guaranteed_profit"], "peak_return_pct": p["return_pct"],
            "peak_at": _iso(ep["peak_at"]),
        })
    return out
