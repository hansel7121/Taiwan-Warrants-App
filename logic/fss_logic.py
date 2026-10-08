"""Forced Short Squeeze paper trading: the pure engine behind the Forced Short Squeeze section.

Reproduces set D of QFS-Pitch-Code "Taiwan Pitch/backtest_noahead.ipynb" as a live rule: every TWSE stock with
a forced short-covering deadline D (last covering day before a short-sale suspension) is scored on D-17 by
days-to-cover (short balance / 20-day average volume). Ex-dividend / ex-rights and AGM deadlines in the top
quintile (Q5) that were public before the close of D-6 are bought at the close of D-6, flipped short at the close
of D and covered at the close of D+5, beta-hedged with TAIEX futures. The quintile edges are rebuilt from every past
deadline whose D fell before the event's D-17 (the notebook's expanding window), so they move as new deadlines pass.

services/fss.py fetches the data and calls into this module; no I/O, Flask or Supabase here. The parsers turn
TWSE/MOPS payloads into plain tuples, Calendar maps dates to trading-day offsets, Panel holds the daily
price/short-balance matrices, and evaluate() turns deadline events into signals and paper trades.
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TW_TZ = ZoneInfo("Asia/Taipei")

# Trade parameters, exactly as backtest_noahead.ipynb.
K, H = 6, 5                    # long from the close of D-6 to D, short from the close of D to D+5
SIG_LAG = 17                   # days-to-cover, turnover and beta are measured on D-17
MIN_VAL20 = 2e7                # 20-day mean turnover (NT$) on D-17
COST_LEG_BPS, HEDGE_BPS = 20, 2
RF = 0.015
BETA_WIN, BETA_MIN, DIMSON_LAGS = 250, 60, 2
Q_PCTS = (0.2, 0.4, 0.6, 0.8)  # quintile edges; Q5 = at or above the 80th percentile
Q_MIN_HIST = 200               # no cutoff until 200 past deadlines are in the pool
EVENT_GAP = 10                 # deadlines for one stock closer than this many trading days are one event
EX_TO_D = 4                    # ex-date - 4 trading days = D
CLOSURE_TO_D = 6               # book-closure start - 6 trading days = D (停止過戶前六個營業日)
CLOSE_AUCTION = time(13, 25)   # a deadline must be public before this on the entry day
HISTORY_DAYS = 330             # trading days of panel needed: 250-day beta window + lags + D-17 + D+5

EXDIV_REASONS = {"除息", "除權息", "除權", "除權、息", "現增除權", "現金增資", "除權息預告"}
AGM_REASON = "股東常會"
TRADE_REASONS = EXDIV_REASONS | {AGM_REASON}

STOCK_RE = re.compile(r"[1-9]\d{3}")
_TAG = re.compile(r"<[^>]+>")


# ── parsing ──────────────────────────────────────────────────────────────────

def _clean(s):
    return _TAG.sub("", str(s)).replace("&nbsp;", "").strip()


def num(s):
    """'1,234.5' -> 1234.5; blanks, '--' and anything unparsable -> nan."""
    try:
        return float(_clean(s).replace(",", ""))
    except ValueError:
        return np.nan


def roc_date(s):
    """ROC or ISO date in any TWSE/MOPS spelling ('115.10.08', '115年10月08日', '1151008', '2026-10-08') -> date or None."""
    s = _clean(s)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(*map(int, m.groups()))
    m = re.fullmatch(r"(\d{2,3})[./年](\d{1,2})[./月](\d{1,2})日?", s) or re.fullmatch(r"(\d{3})(\d{2})(\d{2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return date(y + 1911, mo, d)
    except ValueError:
        return None


def tw_datetime(d, hms):
    """date + 'HH:MM:SS' or 'HHMMSS' (Taipei) -> aware datetime; a missing time counts as end of day."""
    s = _clean(hms or "").replace(":", "")
    t = time(int(s[:2]), int(s[2:4]), int(s[4:6])) if re.fullmatch(r"\d{6}", s) else time(23, 59, 59)
    return datetime.combine(d, t, TW_TZ)


def _table(j, needle):
    for t in (j or {}).get("tables", []) or []:
        if needle in (t.get("title") or ""):
            return t
    return None


def parse_mi_index(j):
    """MI_INDEX type=ALLBUT0999 -> ({stock_id: (name, close, volume_shares, value_ntd)}, taiex_total_return)."""
    t = _table(j, "每日收盤行情")
    prices = {}
    for r in (t or {}).get("data", []):
        sid = r[0].strip()
        if STOCK_RE.fullmatch(sid):
            prices[sid] = (r[1].strip(), num(r[8]), num(r[2]), num(r[4]))
    tr = np.nan
    for tb in (j or {}).get("tables", []) or []:
        for r in tb.get("data", []) or []:
            if r and r[0] == "發行量加權股價報酬指數":
                tr = num(r[1])
    return prices, tr


def parse_mi_margn(j):
    """MI_MARGN selectType=ALL -> {stock_id: short balance in board lots (融券今日餘額)}."""
    t = _table(j, "融資融券彙總")
    return {r[0].strip(): num(r[12]) for r in (t or {}).get("data", []) if STOCK_RE.fullmatch(r[0].strip())}


def parse_twt49u(j):
    """TWT49U ex-rights/ex-dividend results -> [(date, stock_id, adj)], adj = reference price / prior close."""
    out = []
    for r in (j or {}).get("data", []) or []:
        d, sid, before, ref = roc_date(r[0]), r[1].strip(), num(r[3]), num(r[4])
        if d and STOCK_RE.fullmatch(sid) and before > 0 and ref > 0:
            out.append((d, sid, ref / before))
    return out


def parse_holidays(j):
    """holidaySchedule -> set of weekday dates with no trading. Rows named 開始交易/最后交易 are trading days."""
    closed = set()
    for r in (j or {}).get("data", []) or []:
        d = roc_date(r[0])
        if d and "開始交易" not in r[1] and "最後交易" not in r[1]:
            closed.add(d)
    return closed


def parse_bfi84u(j):
    """BFI84U 停券預告 -> [(stock_id, name, D, reason)]; D = 停券起日 (the last covering day)."""
    out = []
    for r in (j or {}).get("data", []) or []:
        sid, d = r[0].strip(), roc_date(r[2])
        if STOCK_RE.fullmatch(sid) and d:
            out.append((sid, r[1].strip(), d, r[4].strip()))
    return out


def parse_twt48u(j):
    """TWT48U 除權除息預告 -> [(stock_id, name, ex_date)]."""
    out = []
    for r in (j or {}).get("data", []) or []:
        sid, d = r[1].strip(), roc_date(r[0])
        if STOCK_RE.fullmatch(sid) and d:
            out.append((sid, r[2].strip(), d))
    return out


def parse_t187ap38(rows):
    """OpenAPI t187ap38_L (shareholder meetings) -> [(stock_id, name, reason, closure_start, announced_at)]."""
    out = []
    for r in rows or []:
        sid = str(r.get("公司代號", "")).strip()
        start = roc_date(r.get("停止過戶起訖日期-起", ""))
        ann = roc_date(r.get("公告日期", ""))
        if not (STOCK_RE.fullmatch(sid) and start):
            continue
        kind = r.get("股東常(臨時)會日期-常或臨時", "")
        reason = AGM_REASON if "常會" in kind else "股東臨時會"
        out.append((sid, str(r.get("公司名稱", "")).strip(), reason, start,
                    tw_datetime(ann, r.get("公告時間")) if ann else None))
    return out


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def parse_t108sb27(html):
    """MOPS t108sb27 -> [(stock_id, name, ex_date, announced_at)]; ex-date = earlier of cells 6 and 10, posting = cells -3/-2."""
    out = []
    for row in _ROW.findall(html or ""):
        c = [_clean(x) for x in _CELL.findall(row)]
        if len(c) < 18 or not STOCK_RE.fullmatch(c[0]):
            continue
        exs = [d for d in (roc_date(c[6]), roc_date(c[10])) if d]
        ann = roc_date(c[-3])
        if exs and ann:
            out.append((c[0], c[1], min(exs), tw_datetime(ann, c[-2])))
    return out


# ── trading calendar ─────────────────────────────────────────────────────────

class Calendar:
    """Past trading days (from the panel) followed by future weekdays that are not exchange holidays."""

    def __init__(self, past_days, closed, horizon=400):
        days = sorted(set(past_days))
        d = days[-1] if days else date.today()
        future = []
        while len(future) < horizon:
            d += timedelta(days=1)
            if d.weekday() < 5 and d not in closed:
                future.append(d)
        self.days = days + future
        self.n_past = len(days)
        self._arr = np.array(self.days, dtype="datetime64[D]")

    def index(self, d):
        """Index of d, or of the first trading day after it."""
        return int(np.searchsorted(self._arr, np.datetime64(d, "D")))

    def at(self, i):
        return self.days[i] if 0 <= i < len(self.days) else None


# ── market panel ─────────────────────────────────────────────────────────────

class Panel:
    """Wide (trading day x stock) matrices built exactly as backtest_noahead.ipynb builds R, SB, ADV and VAL20."""

    def __init__(self, prices, margin, index, adj):
        """Long frames: prices [date, stock_id, name, close, volume, value], margin [.., short_bal], index [date, taiex_tr], adj."""
        idx = index.drop_duplicates("date").sort_values("date").set_index("date")
        cal = pd.DatetimeIndex(idx.index)
        px = prices.drop_duplicates(["date", "stock_id"])
        mg = margin.drop_duplicates(["date", "stock_id"])
        stocks = pd.Index(sorted(set(px.stock_id) & set(mg.stock_id)))
        wide = lambda df, c: df.pivot(index="date", columns="stock_id", values=c).reindex(index=cal, columns=stocks)
        close = wide(px, "close")
        a = wide(adj.drop_duplicates(["date", "stock_id"]), "adj").fillna(1.0) if len(adj) else 1.0
        ret = close / (close.ffill().shift() * a) - 1
        ret[ret.abs() > 0.105] = np.nan
        self.dates = [d.date() for d in cal]
        self.stocks = list(stocks)
        self.col = {s: i for i, s in enumerate(stocks)}
        self.names = px.drop_duplicates("stock_id", keep="last").set_index("stock_id")["name"].to_dict()
        self.close = close.values
        self.R = ret.values
        self.SB = wide(mg, "short_bal").values
        self.ADV = (wide(px, "volume") / 1000).rolling(20, min_periods=10).mean().values
        self.VAL20 = wide(px, "value").rolling(20, min_periods=10).mean().values
        self.taiex = idx.taiex_tr.pct_change(fill_method=None).fillna(0).values
        self.hedge_r = self.taiex - RF / 252
        T = len(cal)
        self.m_lags = np.column_stack([np.r_[np.full(k, np.nan), self.taiex[:T - k]] for k in range(DIMSON_LAGS + 1)])

    @property
    def T(self):
        return len(self.dates)

    def beta(self, j, end):
        """Dimson beta (lags 0-2) on the BETA_WIN days ending at `end`, Blume-shrunk; 1.0 with too little data."""
        rows = slice(max(0, end - BETA_WIN + 1), end + 1)
        y, X = self.R[rows, j], self.m_lags[rows]
        ok = np.isfinite(y) & np.isfinite(X).all(1) & (np.abs(y) < 0.095)
        if ok.sum() < BETA_MIN:
            return 1.0
        b = np.linalg.lstsq(np.column_stack([np.ones(ok.sum()), X[ok]]), y[ok], rcond=None)[0][1:].sum()
        return float(0.67 * b + 0.33)

    def signal(self, sid, sig_i):
        """Short balance, ADV, days-to-cover, turnover and beta on day sig_i; None if not computable."""
        j = self.col.get(sid)
        if j is None or not 0 <= sig_i < self.T:
            return None
        sb, adv, val20 = self.SB[sig_i, j], self.ADV[sig_i, j], self.VAL20[sig_i, j]
        dtc = sb / adv if np.isfinite(sb) and np.isfinite(adv) and adv > 0 else np.nan
        return {"short_bal": _f(sb), "adv20": _f(adv), "dtc": _f(dtc), "val20": _f(val20), "beta": self.beta(j, sig_i)}

    def marks(self, sid, d_i, beta):
        """Daily P&L of one trade (fraction of notional) for every day D-5..D+5 already in the panel."""
        j = self.col[sid]
        cost = COST_LEG_BPS / 1e4 + abs(beta) * HEDGE_BPS / 1e4
        out = []
        for off in range(-K + 1, H + 1):
            t = d_i + off
            if t >= self.T:
                break
            r = float(np.nan_to_num(self.R[t, j]))
            sign = 1.0 if off <= 0 else -1.0
            pnl = sign * (r - beta * self.hedge_r[t]) - (cost if off in (0, H) else 0.0)
            out.append({"date": self.dates[t].isoformat(), "off": off, "side": "long" if off <= 0 else "short",
                        "stock_ret": r, "hedge_ret": float(self.hedge_r[t]), "cost": cost if off in (0, H) else 0.0,
                        "pnl": pnl, "close": _f(self.close[t, j])})
        return out


def _f(x):
    return None if x is None or not np.isfinite(x) else float(x)


# ── quintiles ────────────────────────────────────────────────────────────────

def quintile_edges(dtc):
    """The four quintile edges of a days-to-cover sample (pandas' default linear interpolation)."""
    a = np.asarray([x for x in dtc if x is not None and np.isfinite(x)], dtype=float)
    return [float(x) for x in np.quantile(a, Q_PCTS)] if len(a) else None


def bucket(dtc, edges):
    """1..5; Q5 = dtc at or above the 80th-percentile edge (the notebook's `dtc >= THR`)."""
    if dtc is None or edges is None or not np.isfinite(dtc):
        return None
    return int(np.searchsorted(edges, dtc, side="right")) + 1


class Population:
    """The quintile pool: one row per past deadline (short balance > 0 on D-17) with its D and days-to-cover."""

    def __init__(self, d_dates, dtc):
        order = np.argsort(np.array(d_dates, dtype="datetime64[D]"), kind="stable")
        self.d = np.array(d_dates, dtype="datetime64[D]")[order]
        self.dtc = np.asarray(dtc, dtype=float)[order]

    def edges_as_of(self, sig_date):
        """Quintile edges over deadlines whose D fell before sig_date (expanding window); None during burn-in."""
        n = int(np.searchsorted(self.d, np.datetime64(sig_date, "D"), side="left"))
        return (quintile_edges(self.dtc[:n]) if n >= Q_MIN_HIST else None), n


# ── events → signals → trades ────────────────────────────────────────────────

def is_tradeable(reasons):
    """Set D trades ex-dividend / ex-rights and AGM deadlines only."""
    return bool(set(reasons) & TRADE_REASONS)


def cluster(rows, cal):
    """One stock's rows -> events (rows < EVENT_GAP trading days apart merge; BFI84U's D wins; earliest known_at)."""
    rows = sorted(rows, key=lambda r: r["d_date"])
    out, cur = [], []
    for r in rows:
        if cur and cal.index(r["d_date"]) - cal.index(cur[0]["d_date"]) >= EVENT_GAP:
            out.append(cur)
            cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    events = []
    for g in out:
        exch = [r for r in g if "bfi84u" in (r.get("sources") or {})]
        head = (exch or g)[0]
        known = [r["known_at"] for r in g if r.get("known_at")]
        events.append({**head, "members": [r["id"] for r in g],
                       "reasons": sorted({x for r in g for x in r.get("reasons") or []}),
                       "known_at": min(known) if known else None})
    return events


def evaluate(events, panel, cal, population, start, today):
    """Clustered events -> (one status/signal update per event, one trade per entry); pure, so re-runs are idempotent."""
    last = panel.T - 1
    today_edges, _ = population.edges_as_of(today)
    updates, trades = [], []
    for ev in events:
        d_i = cal.index(ev["d_date"])
        sig_i, entry_i = d_i - SIG_LAG, d_i - K
        up = {"id": ev["id"], "sig_date": _iso(cal.at(sig_i)), "entry_date": _iso(cal.at(entry_i)),
              "exit_date": _iso(cal.at(d_i + H)), "status": None, "note": None,
              "short_bal": None, "adv20": None, "dtc": None, "val20": None, "beta": None,
              "bucket": None, "q80": None}
        updates.append(up)
        if sig_i > last:
            up["q80"] = today_edges[3] if today_edges else None
        if ev["stock_id"] not in panel.col:
            up["status"], up["note"] = "skipped", "not in the TWSE universe"
            continue
        if sig_i < 1:
            up["status"], up["note"] = "skipped", "deadline before the price history"
            continue
        if sig_i > last:
            up["status"] = "watching"
            continue
        sig = panel.signal(ev["stock_id"], sig_i)
        up.update(sig or {})
        edges, _ = population.edges_as_of(cal.at(sig_i))
        up["q80"] = edges[3] if edges else None
        up["bucket"] = bucket(up["dtc"], edges)
        if entry_i > last:
            up["status"] = "signal"
            continue
        entry_date = cal.at(entry_i)
        cutoff = datetime.combine(entry_date, CLOSE_AUCTION, TW_TZ)
        why = []
        if entry_date < start:
            why.append("entry before go-live")
        if not is_tradeable(ev.get("reasons") or []):
            why.append("reason not traded (" + "/".join(ev.get("reasons") or ["?"]) + ")")
        if not ev.get("known_at") or ev["known_at"] >= cutoff:
            why.append("not public before the D-6 close")
        if not (up["short_bal"] or 0) > 0:
            why.append("no short balance")
        if up["bucket"] != 5:
            why.append(f"bucket Q{up['bucket']}" if up["bucket"] else "no days-to-cover")
        if (up["val20"] or 0) < MIN_VAL20:
            why.append("turnover below NT$20m")
        if why:
            up["status"], up["note"] = "skipped", "; ".join(why)
            continue
        up["status"] = "traded"
        trades.append(_trade(ev, up, panel, cal, d_i, entry_i, last))
    return updates, trades


def _trade(ev, up, panel, cal, d_i, entry_i, last):
    j = panel.col[ev["stock_id"]]
    marks = panel.marks(ev["stock_id"], d_i, up["beta"])
    legs = lambda side: sum(m["pnl"] for m in marks if m["side"] == side)
    state = "long" if last < d_i else "short" if last < d_i + H else "closed"
    return {"id": ev["id"], "stock_id": ev["stock_id"], "name": ev.get("name") or panel.names.get(ev["stock_id"]),
            "reasons": "/".join(ev.get("reasons") or []), "d_date": ev["d_date"].isoformat(),
            "entry_date": up["entry_date"], "flip_date": ev["d_date"].isoformat(), "exit_date": up["exit_date"],
            "known_at": ev["known_at"].isoformat(), "state": state, "beta": up["beta"], "dtc": up["dtc"],
            "q80": up["q80"], "val20": up["val20"], "entry_close": _f(panel.close[entry_i, j]),
            "last_close": next((m["close"] for m in reversed(marks) if m["close"] is not None), _f(panel.close[entry_i, j])),
            "long_ret": legs("long"), "short_ret": legs("short"), "net_ret": legs("long") + legs("short"),
            "marks": marks}


def _iso(d):
    return d.isoformat() if d else None


def daily_pnl(trades, notional):
    """Portfolio P&L by date (NT$, fixed notional per trade) plus the number of open trades that day."""
    by = {}
    for t in trades:
        for m in t["marks"]:
            row = by.setdefault(m["date"], {"date": m["date"], "pnl": 0.0, "n_open": 0})
            row["pnl"] += m["pnl"] * notional
            row["n_open"] += 1
    rows = sorted(by.values(), key=lambda r: r["date"])
    cum = 0.0
    for r in rows:
        cum += r["pnl"]
        r["cum"] = cum
    return rows
