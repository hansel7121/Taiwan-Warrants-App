"""Forced Short Squeeze paper trader: data fetching, the on-disk daily panel, and the daily run.

The scheduler calls start_run() a few times each trading day: scrape-only runs during the session record when each
deadline first became public, and the evening full run (after TWSE posts the day's short balances) updates the
panel, rebuilds the quintile cutoff and re-marks every paper trade through logic/fss_logic.evaluate(). The
/fss_run route calls the same start_run(). Runs happen on their own thread, never on the scheduler's sync lock,
because a cold start backfills ~a year and a half of TWSE data at a polite request rate (~30 minutes).

Panel: one gzipped JSON per trading day under FSS_DATA_DIR/panel (prices, short balances, TAIEX total return,
ex-rights adjustments). It defaults to a folder inside LIVE_TICK_LOG_DIR so it lands on the persistent volume.
"""
import gzip
import json
import os
import threading
import time as _time
import traceback
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import requests

from logic import fss_logic as F
from services import db_fss

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TICK_DIR = os.environ.get("LIVE_TICK_LOG_DIR")
DATA_DIR = os.environ.get("FSS_DATA_DIR") or (os.path.join(_TICK_DIR, "fss") if _TICK_DIR else os.path.join(_REPO, "fss_data"))
PANEL_DIR = os.path.join(DATA_DIR, "panel")
CLOSED_FILE = os.path.join(DATA_DIR, "closed_days.json")
HOLIDAY_FILE = os.path.join(DATA_DIR, "holidays_{year}.json")

START = date.fromisoformat(os.environ.get("FSS_START_DATE", "2026-10-08"))   # first entry date that can trade
NOTIONAL = float(os.environ.get("FSS_NOTIONAL", "1000000"))                   # NT$ per trade
KEEP_FILES = F.HISTORY_DAYS + 60
BACKFILL_CAL_DAYS = 520
MARGIN_READY = time(21, 30)    # TWSE posts the day's short balances in the evening

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36", "Referer": "https://www.twse.com.tw/"}
TWSE = "https://www.twse.com.tw/rwd/zh"
MOPS_T108 = "https://mopsov.twse.com.tw/mops/web/ajax_t108sb27"
T187AP38 = "https://openapi.twse.com.tw/v1/opendata/t187ap38_L"

_lock = threading.Lock()
_thread = None


class _Client:
    """requests.Session with a minimum interval between calls (TWSE drops clients below ~1s) and retries."""

    def __init__(self, interval=2.5):
        self.s = requests.Session()
        self.s.headers.update(_HEADERS)
        self.interval, self.last = interval, 0.0

    def _call(self, method, url, **kw):
        delay = 5
        for attempt in range(4):
            wait = self.interval - (_time.time() - self.last)
            if wait > 0:
                _time.sleep(wait)
            self.last = _time.time()
            try:
                r = self.s.request(method, url, timeout=60, **kw)
                if r.status_code == 200:
                    return r
                if r.status_code not in (403, 429) and r.status_code < 500:
                    return None
            except requests.RequestException as e:
                print(f"FSS: {url} attempt {attempt + 1}: {e}", flush=True)
            _time.sleep(delay)
            delay *= 2
        return None

    def json(self, url, **params):
        r = self._call("GET", url, params=params)
        try:
            return r.json() if r is not None else None
        except ValueError:
            return None

    def post_text(self, url, data):
        r = self._call("POST", url, data=data)
        if r is None:
            return None
        r.encoding = "utf-8"
        return r.text


def _now():
    return datetime.now(F.TW_TZ)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ── calendar ─────────────────────────────────────────────────────────────────

def closed_days(cli, years):
    """Exchange holidays for `years` (cached per year, refreshed weekly) plus days found closed (typhoons)."""
    closed = {date.fromisoformat(d) for d in _read_json(CLOSED_FILE, [])}
    for y in years:
        path = HOLIDAY_FILE.format(year=y)
        fresh = os.path.exists(path) and _time.time() - os.path.getmtime(path) < 7 * 86400
        cached = _read_json(path, None)
        if not fresh:
            j = cli.json(f"{TWSE}/holidaySchedule/holidaySchedule", response="json", date=f"{y}0101")
            if j and j.get("data") and str(y - 1911) in (j.get("title") or ""):
                cached = sorted(d.isoformat() for d in F.parse_holidays(j))
                _write_json(path, cached)
        closed |= {date.fromisoformat(d) for d in cached or []}
    return closed


def _mark_closed(d):
    days = set(_read_json(CLOSED_FILE, []))
    days.add(d.isoformat())
    _write_json(CLOSED_FILE, sorted(days))


# ── panel ────────────────────────────────────────────────────────────────────

def _day_path(d):
    return os.path.join(PANEL_DIR, d.strftime("%Y%m%d") + ".json.gz")


def panel_days():
    """Dates with a complete day file, ascending."""
    if not os.path.isdir(PANEL_DIR):
        return []
    return sorted(datetime.strptime(f[:8], "%Y%m%d").date() for f in os.listdir(PANEL_DIR) if f.endswith(".json.gz"))


def write_day(d, prices, short_bal, taiex_tr, adj):
    """One trading day: {stock_id: [name, close, volume, value]}, {stock_id: short_bal}, TAIEX TR, {stock_id: adj}."""
    os.makedirs(PANEL_DIR, exist_ok=True)
    clean = lambda x: None if x is None or (isinstance(x, float) and not np.isfinite(x)) else x
    body = {"date": d.isoformat(), "taiex_tr": clean(taiex_tr), "adj": adj,
            "prices": {k: [v[0]] + [clean(x) for x in v[1:]] for k, v in prices.items()},
            "short_bal": {k: clean(v) for k, v in short_bal.items()}}
    tmp = _day_path(d) + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    os.replace(tmp, _day_path(d))


def update_panel(cli, closed, now):
    """Fetch every missing trading day up to the latest one whose short balances are out. Returns days added."""
    have = panel_days()
    target = now.date() if now.time() >= MARGIN_READY else now.date() - timedelta(days=1)
    start = have[-1] + timedelta(days=1) if have else target - timedelta(days=BACKFILL_CAL_DAYS)
    todo, d = [], start
    while d <= target:
        if d.weekday() < 5 and d not in closed:
            todo.append(d)
        d += timedelta(days=1)
    if not todo:
        return 0
    print(f"FSS: fetching {len(todo)} trading days {todo[0]} -> {todo[-1]}", flush=True)
    adj = {}
    j = cli.json(f"{TWSE}/exRight/TWT49U", startDate=todo[0].strftime("%Y%m%d"),
                 endDate=todo[-1].strftime("%Y%m%d"), response="json")
    if j is None:
        raise RuntimeError("TWT49U (ex-rights reference prices) unavailable")
    for d, sid, a in F.parse_twt49u(j):
        adj.setdefault(d, {})[sid] = a
    added = 0
    for d in todo:
        ymd = d.strftime("%Y%m%d")
        jp = cli.json(f"{TWSE}/afterTrading/MI_INDEX", date=ymd, type="ALLBUT0999", response="json")
        if jp is None:
            raise RuntimeError(f"MI_INDEX {ymd} request failed")
        if jp.get("stat") != "OK":
            if d < now.date():
                _mark_closed(d)          # a weekday with no trading that the holiday list missed (typhoon)
            continue
        jm = cli.json(f"{TWSE}/marginTrading/MI_MARGN", date=ymd, selectType="ALL", response="json")
        if not jm or jm.get("stat") != "OK":
            print(f"FSS: MI_MARGN {ymd} not published yet; stopping", flush=True)
            break
        prices, tr = F.parse_mi_index(jp)
        write_day(d, prices, F.parse_mi_margn(jm), tr, adj.get(d, {}))
        added += 1
    for old in panel_days()[:-KEEP_FILES]:
        os.remove(_day_path(old))
    return added


def load_panel():
    """Panel over the last HISTORY_DAYS day files."""
    days = panel_days()[-F.HISTORY_DAYS:]
    px, mg, ix, ad = [], [], [], []
    for d in days:
        with gzip.open(_day_path(d), "rt", encoding="utf-8") as f:
            b = json.load(f)
        ts = pd.Timestamp(d)
        ix.append((ts, b["taiex_tr"]))
        px.extend((ts, k, v[0], v[1], v[2], v[3]) for k, v in b["prices"].items())
        mg.extend((ts, k, v) for k, v in b["short_bal"].items())
        ad.extend((ts, k, v) for k, v in (b.get("adj") or {}).items())
    return F.Panel(pd.DataFrame(px, columns=["date", "stock_id", "name", "close", "volume", "value"]).astype({"close": float, "volume": float, "value": float}),
                   pd.DataFrame(mg, columns=["date", "stock_id", "short_bal"]).astype({"short_bal": float}),
                   pd.DataFrame(ix, columns=["date", "taiex_tr"]).astype({"taiex_tr": float}),
                   pd.DataFrame(ad, columns=["date", "stock_id", "adj"]))


# ── deadlines ────────────────────────────────────────────────────────────────

def scrape_deadlines(cli, now):
    """Deadlines the public sources list now -> ([{stock_id, name, source, reason, kind, date, announced_at}], per-source status)."""
    obs, status = [], {}

    def take(name, fn):
        try:
            rows = fn()
            obs.extend(rows)
            status[name] = len(rows)
        except Exception as e:
            status[name] = f"error: {e}"
            print(f"FSS: source {name} failed: {e}", flush=True)

    def bfi():
        j = cli.json(f"{TWSE}/marginTrading/BFI84U", response="json")
        if not j or j.get("stat") != "OK":
            raise RuntimeError("no data")
        return [dict(stock_id=s, name=n, source="bfi84u", reason=r, kind="d", date=d, announced_at=None)
                for s, n, d, r in F.parse_bfi84u(j)]

    def twt48():
        j = cli.json(f"{TWSE}/exRight/TWT48U", response="json")
        if not j or j.get("stat") != "OK":
            raise RuntimeError("no data")
        return [dict(stock_id=s, name=n, source="twt48u", reason="除權息預告", kind="ex", date=d, announced_at=None)
                for s, n, d in F.parse_twt48u(j)]

    def agm():
        r = cli._call("GET", T187AP38)
        if r is None:
            raise RuntimeError("no data")
        return [dict(stock_id=s, name=n, source="t187ap38", reason=reason, kind="closure", date=d, announced_at=a)
                for s, n, reason, d, a in F.parse_t187ap38(r.json())]

    def mops():
        out = []
        years = [now.year - 1, now.year] if now.month <= 2 else [now.year]
        for y in years:
            html = cli.post_text(MOPS_T108, {"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
                                             "TYPEK": "sii", "year": y - 1911, "type": ""})
            if html is None or "查詢過於頻繁" in html:
                raise RuntimeError("MOPS rate-limited or unavailable")
            out += [dict(stock_id=s, name=n, source="mops_t108", reason="除權息", kind="ex", date=d, announced_at=a)
                    for s, n, d, a in F.parse_t108sb27(html)]
        return out

    take("bfi84u", bfi)
    take("twt48u", twt48)
    take("t187ap38", agm)
    take("mops_t108", mops)
    return obs, status


def to_deadline(o, cal):
    """Observation -> deadline D: as listed (BFI84U), ex-date - 4 or book-closure start - 6 trading days."""
    if o["kind"] == "d":
        return o["date"]
    back = F.EX_TO_D if o["kind"] == "ex" else F.CLOSURE_TO_D
    return cal.at(cal.index(o["date"]) - back)


def merge_observations(existing, obs, cal, now, horizon_start):
    """Fold scraped observations into event rows keyed {stock_id}:{D}; first-seen times only ever move earlier."""
    rows = {r["id"]: dict(r) for r in existing}
    seen = now.isoformat()
    changed = set()
    for o in obs:
        d = to_deadline(o, cal)
        if d is None or d < horizon_start:
            continue
        key = f"{o['stock_id']}:{d.isoformat()}"
        r = rows.get(key) or {"id": key, "stock_id": o["stock_id"], "name": o["name"], "d_date": d.isoformat(),
                              "reasons": [], "sources": {}, "known_at": None}
        before = json.dumps([r.get("reasons"), r.get("sources"), r.get("known_at")], sort_keys=True, default=str)
        r["reasons"] = sorted(set(r.get("reasons") or []) | {o["reason"]})
        src = dict(r.get("sources") or {})
        src[o["source"]] = min(src.get(o["source"], seen), seen)
        r["sources"] = src
        cands = [x for x in (r.get("known_at"), o["announced_at"].isoformat() if o["announced_at"] else None, seen) if x]
        r["known_at"] = min(cands, key=lambda x: datetime.fromisoformat(x))
        r["name"] = r.get("name") or o["name"]
        if json.dumps([r["reasons"], r["sources"], r["known_at"]], sort_keys=True, default=str) != before:
            changed.add(key)
        rows[key] = r
    return [rows[k] for k in changed]


# ── the run ──────────────────────────────────────────────────────────────────

def _to_event(r):
    return {**r, "d_date": date.fromisoformat(r["d_date"]),
            "known_at": datetime.fromisoformat(r["known_at"]) if r.get("known_at") else None}


def run(kind="full", now=None):
    """One run. kind='scrape' only records deadline sightings; 'full' also updates the panel and re-marks trades."""
    if not _lock.acquire(blocking=False):
        return False
    t0 = _time.time()
    now = now or _now()
    run_at, fields = None, {}
    try:
        run_at = db_fss.start_run(kind)
        cli = _Client()
        closed = closed_days(cli, [now.year - 2, now.year - 1, now.year, now.year + 1])
        if kind == "full":
            update_panel(cli, closed, now)
        days = panel_days()
        if not days:
            raise RuntimeError("no panel data yet")
        cal = F.Calendar(days, closed)
        horizon = cal.at(max(0, cal.index(now.date()) - 30))
        existing = db_fss.list_events(since=horizon)
        obs, sources = scrape_deadlines(cli, now)
        changed = merge_observations(existing, obs, cal, now, horizon)
        if changed:
            db_fss.upsert_events(changed)
        fields.update(sources=sources)
        if kind == "full":
            fields.update(evaluate(closed, now))
        fields.update(status="ok", panel_last=days[-1].isoformat(), as_of=now.date().isoformat())
        return True
    except Exception as e:
        traceback.print_exc()
        fields.update(status="error", error=str(e)[:2000])
        return False
    finally:
        fields["runtime_s"] = round(_time.time() - t0, 1)
        try:
            if run_at:
                db_fss.finish_run(run_at, fields)
        except Exception as e:
            print(f"FSS: could not log the run: {e}", flush=True)
        finally:
            _lock.release()
        print(f"FSS: {kind} run {fields.get('status')} in {fields['runtime_s']}s", flush=True)


def evaluate(closed, now):
    """Score every live event against the panel and upsert events + trades. Returns run-log fields."""
    panel = load_panel()
    cal = F.Calendar(panel.dates, closed)    # trading-day offsets must line up with the panel's rows
    rows = [_to_event(r) for r in db_fss.list_events()]
    pool = db_fss.list_pool()
    seed_end = max((date.fromisoformat(p["d_date"]) for p in pool), default=date.min)

    by_stock = {}
    for r in rows:
        by_stock.setdefault(r["stock_id"], []).append(r)
    clusters, merged = [], []
    for sid, rs in by_stock.items():
        for c in F.cluster(rs, cal):
            clusters.append(c)
            merged += [(m, c["id"]) for m in c["members"] if m != c["id"]]

    # live deadlines join the pool once their days-to-cover exists; the seed covers everything up to seed_end
    pool_d = [date.fromisoformat(p["d_date"]) for p in pool]
    pool_x = [float(p["dtc"]) for p in pool]
    for c in clusters:
        sig_i = cal.index(c["d_date"]) - F.SIG_LAG
        if c["d_date"] > seed_end and 0 < sig_i < panel.T:
            s = panel.signal(c["stock_id"], sig_i)
            if s and (s["short_bal"] or 0) > 0 and s["dtc"] is not None:
                pool_d.append(c["d_date"])
                pool_x.append(s["dtc"])
    population = F.Population(pool_d, pool_x)
    updates, trades = F.evaluate(clusters, panel, cal, population, START, now.date())

    stamp = now.isoformat()
    names = {c["id"]: c.get("name") for c in clusters}
    ev_rows = [{**u, "cluster_id": None, "name": names.get(u["id"]) or panel.names.get(u["id"].split(":")[0]),
                "updated_at": stamp} for u in updates]
    ev_rows += [{"id": m, "cluster_id": head, "status": "merged", "updated_at": stamp} for m, head in merged]
    for r in ev_rows:
        r.setdefault("stock_id", r["id"].split(":")[0])
        r.setdefault("d_date", r["id"].split(":")[1])
    db_fss.upsert_events(ev_rows)
    for t in trades:
        t.update(notional=NOTIONAL, pnl_twd=t["net_ret"] * NOTIONAL, updated_at=stamp)
    if trades:
        db_fss.upsert_trades(trades)
    edges, n = population.edges_as_of(now.date())
    return {"n_events": len(clusters), "n_trades": len(trades), "pool_n": n, "edges": edges}


def start_run(kind="full"):
    """Run on a background thread unless one is already going. Returns False if one was."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _thread = threading.Thread(target=run, args=(kind,), name=f"fss-{kind}", daemon=True)
    _thread.start()
    return True


def is_running():
    return _thread is not None and _thread.is_alive()


def state():
    """Everything the Forced Short Squeeze section shows."""
    today = _now().date()
    events = db_fss.list_events(since=today - timedelta(days=45))
    trades = db_fss.list_trades()
    pool = db_fss.list_pool()
    full_pool = [(date.fromisoformat(p["d_date"]), float(p["dtc"])) for p in pool]
    seed_end = max((d for d, _ in full_pool), default=date.min)
    full_pool += [(date.fromisoformat(e["d_date"]), float(e["dtc"])) for e in db_fss.list_events()
                  if e.get("status") != "merged" and e.get("dtc") is not None and (e.get("short_bal") or 0) > 0
                  and date.fromisoformat(e["d_date"]) > seed_end]
    population = F.Population([d for d, _ in full_pool], [x for _, x in full_pool])
    edges, n = population.edges_as_of(today)
    daily = F.daily_pnl([t for t in trades if t.get("marks")], NOTIONAL)
    return {"events": [e for e in events if e.get("status") != "merged"], "trades": trades, "daily": daily,
            "edges": edges, "pool_n": n, "runs": db_fss.list_runs(), "running": is_running(),
            "config": {"start": START.isoformat(), "notional": NOTIONAL, "K": F.K, "H": F.H, "sig_lag": F.SIG_LAG,
                       "min_val20": F.MIN_VAL20, "cost_leg_bps": F.COST_LEG_BPS, "hedge_bps": F.HEDGE_BPS,
                       "q_min_hist": F.Q_MIN_HIST}}
