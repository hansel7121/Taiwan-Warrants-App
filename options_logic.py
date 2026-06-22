import io
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from warrant_logic import implied_vol, bs_delta, calc_real_leverage

R = 0.01875  # Taiwan CBC benchmark rate

TAIFEX_URL = "https://www.taifex.com.tw/cht/3/optDataDown"

# TSMC individual stock options (commodity_id "2330") return no data from
# TAIFEX's optDataDown endpoint — they appear to use a separate API.
# Only TXO (TAIEX index options) is supported here.
COMMODITY_MAP = {
    "TXO": {"commodity_id": "TXO", "ticker": "^TWII", "exercise_ratio": 50},
}


def _decode(content):
    for enc in ("big5", "cp950", "utf-8-sig", "utf-8"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("big5", errors="replace")


def _fetch_taifex(commodity_id):
    today = pd.Timestamp.today()
    for i in range(7):
        d = today - pd.Timedelta(days=i)
        r = requests.post(
            TAIFEX_URL,
            data={
                "down_type": "1",
                "commodity_id": commodity_id,
                "queryStartDate": d.strftime("%Y/%m/%d"),
                "queryEndDate": d.strftime("%Y/%m/%d"),
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": TAIFEX_URL},
            timeout=15,
        )
        r.raise_for_status()
        text = _decode(r.content)
        lines = [l for l in text.strip().splitlines() if l.strip()]
        if len(lines) > 2:
            df = pd.read_csv(io.StringIO(text))
            if len(df) > 0:
                # Verify at least some settlement prices are usable
                settle_col = next((c for c in df.columns if "結算" in c.strip()), None)
                if settle_col:
                    settle = pd.to_numeric(
                        df[settle_col].astype(str).str.strip().replace("-", ""),
                        errors="coerce",
                    )
                    if (settle > 0).sum() < 10:
                        continue  # weekend/holiday: settlement data not yet available
                return df
    raise RuntimeError(f"No data for {commodity_id} in last 7 trading days")


def _clean_num(series, fill=np.nan):
    return pd.to_numeric(
        series.astype(str).str.strip().replace("-", str(fill)), errors="coerce"
    )


def _parse_and_compute(raw_df, underlying_price, exercise_ratio):
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
            }
        )

    return pd.DataFrame(rows)


def fetch_options(
    stock_codes,
    option_type="All",
    min_days=0,
    max_days=365,
    min_leverage=0,
    min_volume=0,
):
    dfs = []
    errors = []
    for code in stock_codes:
        if code not in COMMODITY_MAP:
            errors.append(f"{code}: not supported")
            continue
        cfg = COMMODITY_MAP[code]
        try:
            S = yf.Ticker(cfg["ticker"]).fast_info["last_price"]
            raw = _fetch_taifex(cfg["commodity_id"])
            df = _parse_and_compute(raw, S, cfg["exercise_ratio"])
            if not df.empty:
                dfs.append(df)
        except Exception as e:
            errors.append(f"{code}: {e}")

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
