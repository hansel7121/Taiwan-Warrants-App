import io
import re
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from warrant_logic import implied_vol, bs_delta, calc_real_leverage

R = 0.01875  # Taiwan CBC benchmark rate

TAIFEX_URL = "https://www.taifex.com.tw/cht/3/optDataDown"

COMMODITY_MAP = {
    "TXO":  {"commodity_ids": ["TXO"],         "ticker": "^TWII",   "exercise_ratio": 50},
    "2330": {"commodity_ids": ["CDA", "CDO"],   "ticker": "2330.TW", "exercise_ratio": 2000},
    "2303": {"commodity_ids": ["CCO"],          "ticker": "2303.TW", "exercise_ratio": 2000},
    "2603": {"commodity_ids": ["CZA", "CZO"],   "ticker": "2603.TW", "exercise_ratio": 2000},
    "2881": {"commodity_ids": ["CEO"],          "ticker": "2881.TW", "exercise_ratio": 2000},
    "2882": {"commodity_ids": ["CKO"],          "ticker": "2882.TW", "exercise_ratio": 2000},
}

# Module-level cache: (commodity_id -> (timestamp, DataFrame))
_taifex_cache: dict = {}
_spot_cache: dict = {}
_CACHE_TTL = 1800  # 30 minutes


def _decode(content):
    for enc in ("big5", "cp950", "utf-8-sig", "utf-8"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("big5", errors="replace")


def _fetch_taifex(commodity_ids: list[str]) -> pd.DataFrame:
    cache_key = ",".join(commodity_ids)
    now = time.time()
    if cache_key in _taifex_cache:
        ts, cached_df = _taifex_cache[cache_key]
        if now - ts < _CACHE_TTL:
            return cached_df

    today = pd.Timestamp.today()
    start = (today - pd.Timedelta(days=7)).strftime("%Y/%m/%d")
    end = today.strftime("%Y/%m/%d")

    frames = []
    for cid in commodity_ids:
        r = requests.post(
            TAIFEX_URL,
            data={"down_type": "1", "commodity_id": cid,
                  "queryStartDate": start, "queryEndDate": end},
            headers={"User-Agent": "Mozilla/5.0", "Referer": TAIFEX_URL},
            timeout=15,
        )
        r.raise_for_status()
        text = _decode(r.content)
        lines = [l for l in text.strip().splitlines() if l.strip()]
        if len(lines) > 2:
            frames.append(pd.read_csv(io.StringIO(text)))

    if not frames:
        raise RuntimeError(f"No data for {cache_key} in last 7 trading days")

    df = pd.concat(frames, ignore_index=True)

    # Find the most recent date that has valid settlement prices
    date_col = next((c for c in df.columns if "交易日期" in c.strip()), None)
    settle_col = next((c for c in df.columns if "結算" in c.strip()), None)
    if date_col and settle_col:
        df["_settle_num"] = pd.to_numeric(
            df[settle_col].astype(str).str.strip().replace("-", ""), errors="coerce"
        )
        dates_with_data = df[df["_settle_num"] > 0].groupby(date_col).size()
        if dates_with_data.empty:
            raise RuntimeError(f"No usable settlement data for {cache_key}")
        latest = dates_with_data.index.max()
        df = df[df[date_col] == latest].drop(columns=["_settle_num"])
    else:
        df = df.drop(columns=["_settle_num"], errors="ignore")

    _taifex_cache[cache_key] = (now, df)
    return df


def _get_spot(ticker):
    now = time.time()
    if ticker in _spot_cache:
        ts, price = _spot_cache[ticker]
        if now - ts < _CACHE_TTL:
            return price
    price = yf.Ticker(ticker).fast_info["last_price"]
    _spot_cache[ticker] = (now, price)
    return price


def _clean_num(series, fill=np.nan):
    return pd.to_numeric(
        series.astype(str).str.strip().replace("-", str(fill)), errors="coerce"
    )


def _parse_and_compute(raw_df, underlying_price, exercise_ratio, compute_iv=True):
    df = raw_df.copy()
    df.columns = df.columns.str.strip()

    colmap = {}
    for c in df.columns:
        cl = c.strip()
        if "交易日期" in cl:
            colmap[c] = "date"
        elif "契約到期日" in cl:
            colmap[c] = "expiry_date"
        elif "履約價" in cl:
            colmap[c] = "strike"
        elif "買賣權" in cl:
            colmap[c] = "type"
        elif "成交量" in cl:
            colmap[c] = "volume"
        elif "結算價" in cl:
            colmap[c] = "settlement"
        elif "未沖銷契約數" in cl:
            colmap[c] = "oi"
        elif "最後最佳買價" in cl:
            colmap[c] = "bid"
        elif "最後最佳賣價" in cl:
            colmap[c] = "ask"
        elif "交易時段" in cl:
            colmap[c] = "session"
    df = df.rename(columns=colmap)

    # Prefer regular session ("一般") to avoid duplicates; fall back to settlement-only (盤後) data
    if "session" in df.columns:
        sessions = df["session"].astype(str).str.strip()
        if (sessions == "一般").any():
            df = df[sessions == "一般"]

    df["type"] = (
        df["type"]
        .astype(str)
        .str.strip()
        .replace({"買權": "Call", "賣權": "Put", "C": "Call", "P": "Put"})
    )

    df["date"] = pd.to_datetime(df["date"].astype(str).str.strip())
    raw_expiry = df["expiry_date"].astype(str).str.strip()
    df["expiry_date"] = pd.to_datetime(raw_expiry, format="%Y%m%d", errors="coerce")
    still_na = df["expiry_date"].isna()
    if still_na.any():
        df.loc[still_na, "expiry_date"] = pd.to_datetime(
            raw_expiry[still_na], errors="coerce"
        )
    df["days_to_expiry"] = (df["expiry_date"] - df["date"]).dt.days

    df["strike"] = _clean_num(df["strike"])
    df["volume"] = _clean_num(df["volume"], fill=0).fillna(0)
    df["settlement"] = _clean_num(df["settlement"])
    df["oi"] = (
        _clean_num(df["oi"], fill=0).fillna(0) if "oi" in df.columns else 0
    )
    df["bid"] = _clean_num(df["bid"]) if "bid" in df.columns else np.nan
    df["ask"] = _clean_num(df["ask"]) if "ask" in df.columns else np.nan

    # Flag live quotes BEFORE any fallback
    df["ask_live"] = df["ask"] > 0
    df["bid_live"] = df["bid"] > 0
    df["is_live"] = df["ask_live"] & df["bid_live"]

    # Fall back to settlement when last-best bid/ask is missing
    df["ask"] = df["ask"].where(df["ask"] > 0, df["settlement"])
    df["bid"] = df["bid"].where(df["bid"] > 0, df["settlement"])

    df = df[
        df["settlement"].notna()
        & (df["settlement"] > 0)
        & (df["days_to_expiry"] > 0)
        & df["strike"].notna()
    ]

    S = underlying_price
    rows = []
    for _, row in df.iterrows():
        K = row["strike"]
        T = row["days_to_expiry"] / 365.0
        is_put = row["type"] == "Put"
        ask = float(row["ask"]) if pd.notna(row["ask"]) and row["ask"] > 0 else np.nan
        bid = float(row["bid"]) if pd.notna(row["bid"]) and row["bid"] > 0 else np.nan

        if T <= 0 or S <= 0 or K <= 0 or np.isnan(ask):
            continue

        if compute_iv:
            iv_ask = implied_vol(ask, S, K, T, R, 1.0, is_put)
            iv_bid = (
                implied_vol(bid, S, K, T, R, 1.0, is_put)
                if not np.isnan(bid)
                else np.nan
            )
            if np.isnan(iv_ask) and not np.isnan(iv_bid):
                iv_ask = iv_bid
            if np.isnan(iv_ask):
                continue

            delta = bs_delta(S, K, T, R, iv_ask, 1.0, is_put)
            leverage = calc_real_leverage(S, abs(delta), ask)
        else:
            # Arb finder does not use IV/delta/leverage — skip the solve so an
            # option is never dropped just because IV wouldn't converge.
            iv_ask = iv_bid = delta = leverage = np.nan

        intrinsic = max(0, K - S) if is_put else max(0, S - K)
        time_value_am = round(ask - intrinsic, 2)

        expiry_label = (
            row["expiry_date"].strftime("%b%y")
            if pd.notna(row["expiry_date"])
            else ""
        )
        contract = f"{'P' if is_put else 'C'}{int(K)} {expiry_label}"

        rows.append(
            {
                "contract": contract,
                "type": row["type"],
                "underlying_price": round(S, 2),
                "ask": round(ask, 2),
                "bid": round(bid, 2) if not np.isnan(bid) else None,
                "days_to_expiry": int(row["days_to_expiry"]),
                "strike": int(K),
                "exercise_ratio": exercise_ratio,
                "volume": int(row["volume"]),
                "oi": int(row["oi"]),
                "time_value_am": time_value_am,
                "iv_ask": round(iv_ask, 4),
                "iv_bid": round(iv_bid, 4) if not np.isnan(iv_bid) else None,
                "delta_calc": round(delta, 4),
                "leverage_calc": round(leverage, 4)
                if not np.isnan(leverage)
                else None,
                "is_live": bool(row["is_live"]),
            }
        )

    return pd.DataFrame(rows)


# ── TAIFEX MIS intraday quotes (~20 min delayed) ────────────────────────────
# The EOD data-download file (_fetch_taifex) is up to a day stale. MIS serves
# the same public quotes intraday via a JSON API — the freshest free TW-option
# source. We use it as the PRIMARY source and fall back to EOD on any failure.
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
_MIS_TTL = 60  # intraday: refresh often
_mis_cache: dict = {}

# Contract SymbolID = [3-char product][5-digit strike][month-letter][week/yr].
#   month-letter A–L = call Jan–Dec, M–X = put Jan–Dec.
#   strike: raw for the index (TXO/TX#), /10 for single-stock options.
_MIS_SYM_RE = re.compile(r"^([A-Z0-9]{3})(\d{5})([A-X])([A-Z0-9])-O$")


def _mis_third_wednesday(year, month):
    d = pd.Timestamp(year=year, month=month, day=1)
    # 0=Mon..2=Wed; first Wednesday then +14 days.
    first_wed = 1 + ((2 - d.dayofweek) % 7)
    return pd.Timestamp(year=year, month=month, day=first_wed + 14)


def _decode_mis_symbol(symbol, disp_cname, is_index):
    m = _MIS_SYM_RE.match(symbol)
    if not m:
        return None
    _prod, strike_s, mon_letter, _last = m.groups()
    li = ord(mon_letter) - ord("A")
    is_put = li >= 12
    month = (li % 12) + 1
    strike = int(strike_s) if is_index else int(strike_s) / 10.0
    # Expiry: weeklies print "W# (YYYY/MM/DD)" in DispCName; monthlies expire on
    # the 3rd Wednesday of the coded month and carry no W# tag.
    wk = re.search(r"W(\d)", disp_cname or "")
    week = wk.group(1) if wk else None
    dm = re.search(r"(\d{4})/(\d{2})/(\d{2})", disp_cname or "")
    if dm:
        expiry = pd.Timestamp(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
    else:
        base = pd.Timestamp.today().year
        base -= base % 10
        year = base + int(_last) if _last.isdigit() else pd.Timestamp.today().year
        if year < pd.Timestamp.today().year - 2:
            year += 10
        expiry = _mis_third_wednesday(year, month)
    return {"strike": strike, "is_put": is_put, "expiry": expiry, "week": week}


def _mis_num(v):
    try:
        f = float(str(v).replace(",", ""))
        return f if f > 0 else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _fetch_mis_quotes(cid, kind):
    """Raw MIS QuoteList for one product code, with a short in-proc cache."""
    key = (cid, kind)
    now = time.time()
    if key in _mis_cache and now - _mis_cache[key][0] < _MIS_TTL:
        return _mis_cache[key][1]
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
               "Referer": "https://mis.taifex.com.tw/futures/"}
    rows = []
    page = 1
    while page <= 20:
        body = {"MarketType": "0", "SymbolType": "O", "KindID": kind, "CID": cid,
                "ExpireMonth": "", "RowSize": "500", "PageNo": str(page)}
        r = requests.post(MIS_URL, json=body, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("RtCode") != "0":
            raise RuntimeError(f"MIS RtCode {data.get('RtCode')}")
        ql = data.get("RtData", {}).get("QuoteList", [])
        rows.extend(ql)
        total = int(data.get("RtData", {}).get("QuoteCount", "0") or 0)
        if len(rows) >= total or not ql:
            break
        page += 1
    _mis_cache[key] = (now, rows)
    return rows


def fetch_options_mis(code, compute_iv=True):
    """Intraday (~20 min delayed) TW option chain from TAIFEX MIS, in the same
    schema as _parse_and_compute so callers are unchanged. Raises on failure."""
    cfg = COMMODITY_MAP[code]
    is_index = code == "TXO"
    kind = "1" if is_index else "4"
    r_free = R
    ratio = cfg["exercise_ratio"]

    all_q = []
    for cid in cfg["commodity_ids"]:
        all_q.extend(_fetch_mis_quotes(cid, kind))
    if not all_q:
        raise RuntimeError("MIS returned no quotes")

    # Underlying spot from the 現貨 (-Q/-S) row; fall back to yfinance.
    spot = float("nan")
    for q in all_q:
        if "現貨" in (q.get("DispCName") or ""):
            spot = _mis_num(q.get("CLastPrice")) or _mis_num(q.get("CRefPrice"))
            if pd.notna(spot):
                break
    if pd.isna(spot) or spot <= 0:
        spot = _get_spot(cfg["ticker"])

    today = pd.Timestamp.today().normalize()
    quote_time = ""
    rows = []
    for q in all_q:
        sym = q.get("SymbolID", "")
        if not sym.endswith("-O"):
            continue
        dec = _decode_mis_symbol(sym, q.get("DispCName", ""), is_index)
        if dec is None:
            continue
        dte = int((dec["expiry"] - today).days)
        if dte <= 0:
            continue
        bid = _mis_num(q.get("CBestBidPrice"))
        if pd.isna(bid):
            bid = _mis_num(q.get("CBidPrice1"))
        ask = _mis_num(q.get("CBestAskPrice"))
        if pd.isna(ask):
            ask = _mis_num(q.get("CAskPrice1"))
        last = _mis_num(q.get("CLastPrice"))
        settle = _mis_num(q.get("SettlementPrice"))
        is_live = bool(pd.notna(bid) and pd.notna(ask))
        # Fall back ask -> last -> settle so a leg is never dropped off-hours.
        if pd.isna(ask):
            ask = last if pd.notna(last) else settle
        if pd.isna(bid):
            bid = last if pd.notna(last) else settle
        if pd.isna(ask) or ask <= 0 or spot <= 0:
            continue
        K = dec["strike"]
        is_put = dec["is_put"]
        T = dte / 365.0
        quote_time = q.get("CTime") or quote_time

        if compute_iv:
            iv_ask = implied_vol(ask, spot, K, T, r_free, ratio, is_put)
            iv_bid = implied_vol(bid, spot, K, T, r_free, ratio, is_put) if pd.notna(bid) else np.nan
            if np.isnan(iv_ask) and not np.isnan(iv_bid):
                iv_ask = iv_bid
            if np.isnan(iv_ask):
                continue
            delta = bs_delta(spot, K, T, r_free, iv_ask, ratio, is_put)
            leverage = calc_real_leverage(spot, abs(delta), ask)
        else:
            iv_ask = iv_bid = delta = leverage = np.nan

        intrinsic = max(0, K - spot) if is_put else max(0, spot - K)
        time_value_am = round(ask - intrinsic, 4)
        # Full expiry date + weekly tag (blank = monthly / 3rd-Wed) so weeklies
        # in the same month are distinct and obviously not the monthly.
        exp_label = dec["expiry"].strftime("%d%b%y")
        wk_label = f" W{dec['week']}" if dec.get("week") else ""
        contract = f"{'P' if is_put else 'C'}{K:g} {exp_label}{wk_label}"
        rows.append({
            "contract": contract, "type": "Put" if is_put else "Call",
            "underlying_price": round(spot, 4), "ask": round(ask, 4),
            "bid": round(bid, 4) if pd.notna(bid) else None,
            "days_to_expiry": dte, "strike": K, "exercise_ratio": ratio,
            "volume": int(_mis_num(q.get("CTotalVolume")) or 0) if pd.notna(_mis_num(q.get("CTotalVolume"))) else 0,
            "oi": int(_mis_num(q.get("OpenInterest")) or 0) if pd.notna(_mis_num(q.get("OpenInterest"))) else 0,
            "time_value_am": time_value_am,
            "iv_ask": round(iv_ask, 4) if pd.notna(iv_ask) else None,
            "iv_bid": round(iv_bid, 4) if pd.notna(iv_bid) else None,
            "delta_calc": round(delta, 4) if pd.notna(delta) else None,
            "leverage_calc": round(leverage, 4) if pd.notna(leverage) else None,
            "is_live": is_live,
            "quote_time": quote_time,
        })
    if not rows:
        raise RuntimeError("MIS: no decodable contracts")
    return pd.DataFrame(rows)


def fetch_options(
    stock_codes,
    option_type="All",
    min_days=0,
    max_days=365,
    min_leverage=0,
    min_volume=0,
    compute_iv=True,
):
    dfs = []
    errors = []
    for code in stock_codes:
        if code not in COMMODITY_MAP:
            errors.append(f"{code}: not supported")
            continue
        cfg = COMMODITY_MAP[code]
        df = None
        # Primary: MIS intraday quotes. Fallback: EOD data-download file.
        try:
            df = fetch_options_mis(code, compute_iv=compute_iv)
        except Exception as e:
            errors.append(f"{code}: MIS {e}")
            df = None
        if df is None or df.empty:
            try:
                S = _get_spot(cfg["ticker"])
                raw = _fetch_taifex(cfg["commodity_ids"])
                df = _parse_and_compute(raw, S, cfg["exercise_ratio"], compute_iv=compute_iv)
            except Exception as e:
                errors.append(f"{code}: EOD {e}")
                df = None
        if df is not None and not df.empty:
            dfs.append(df)

    if not dfs:
        err_msg = "; ".join(errors) if errors else "No data returned"
        raise RuntimeError(err_msg)

    result = pd.concat(dfs, ignore_index=True)

    result = result[
        result["days_to_expiry"].between(int(min_days), int(max_days))
    ]
    if float(min_leverage) > 0:
        result = result[result["leverage_calc"] >= float(min_leverage)]
    if float(min_volume) > 0:
        result = result[result["volume"] >= float(min_volume)]
    if option_type != "All":
        result = result[result["type"] == option_type]

    return result.sort_values(["days_to_expiry", "strike"]).reset_index(drop=True)
