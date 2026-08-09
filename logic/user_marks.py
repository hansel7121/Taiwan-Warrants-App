"""Current marks for a user's starred instruments and position legs.

The Dashboard tab needs one quote per instrument, not a whole scanner table, so
this reuses warrant_logic.read_warrant / options_logic.read_tw_option (and their
snapshot caches) and picks out only the rows asked for. Keyed "kind:code" to
match what static/js/dashboard.js sends. Pure read path — no persistence, no
Flask.
"""
from logic import options_logic
from logic import warrant_logic

# Wide filter bounds: the scanner defaults exist to trim a browse table, and
# would silently drop an instrument the user actually holds (a deep-OTM warrant
# can carry >100% time value, an expiring one <1 DTE).
_WIDE = {"option_type": "All", "min_days": 0, "max_days": 3650,
         "min_leverage": 0.0, "min_volume": 0}


def _key(kind, code):
    return f"{kind}:{code}"


def _num(v):
    """None for NaN/inf so the value survives jsonify and compares cleanly in JS."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _warrant_quotes(instruments, extra_codes=()):
    """(quotes, spots). extra_codes pulls in stocks needed only for their spot."""
    codes = {i.get("underlying_code") for i in instruments if i.get("underlying_code")}
    codes |= {c for c in extra_codes if c}
    if not codes:
        return {}, {}
    df, _err, _meta = warrant_logic.read_warrant(
        sorted(codes), max_tv_pct=1e9, keep_noniv=True, **_WIDE)
    if df is None or df.empty:
        return {}, {}
    wanted = {i["code"] for i in instruments}
    out, spots = {}, {}
    for row in df.to_dict(orient="records"):
        code = str(row.get("warrant_code"))
        spot = _num(row.get("underlying_price"))
        if row.get("underlying_code") and spot is not None:
            spots.setdefault(row["underlying_code"], spot)
        if code not in wanted:
            continue
        out[_key("warrant", code)] = {
            "bid": _num(row.get("bid")), "ask": _num(row.get("ask")),
            "iv": _num(row.get("iv_ask")),
            "underlying_price": _num(row.get("underlying_price")),
            "strike": _num(row.get("strike")),
            "days_to_expiry": _num(row.get("days_to_expiry")),
            "exercise_ratio": _num(row.get("exercise_ratio")),
            "type": row.get("type"), "name": row.get("warrant_name"),
            "underlying_code": row.get("underlying_code"),
        }
    return out, spots


def _tw_option_quotes(instruments):
    """(quotes, spots) — same shape as _warrant_quotes."""
    codes = {i.get("underlying_code") for i in instruments if i.get("underlying_code")}
    if not codes:
        return {}, {}
    df, _err, _meta = options_logic.read_tw_option(
        sorted(codes), keep_noniv=True, **_WIDE)
    if df is None or df.empty:
        return {}, {}
    wanted = {i["code"] for i in instruments}
    out, spots = {}, {}
    for row in df.to_dict(orient="records"):
        contract = str(row.get("contract"))
        spot = _num(row.get("underlying_price"))
        if row.get("stock_code") and spot is not None:
            spots.setdefault(row["stock_code"], spot)
        if contract not in wanted:
            continue
        out[_key("tw_option", contract)] = {
            "bid": _num(row.get("bid")), "ask": _num(row.get("ask")),
            "iv": _num(row.get("iv_ask")),
            "underlying_price": _num(row.get("underlying_price")),
            "strike": _num(row.get("strike")),
            "days_to_expiry": _num(row.get("days_to_expiry")),
            "contract_size": _num(row.get("exercise_ratio")),
            "type": row.get("type"), "name": contract,
            "underlying_code": row.get("stock_code"),
        }
    return out, spots


def quotes(instruments):
    """Marks for [{kind, code, underlying_code}], keyed "kind:code".

    An 'underlying' instrument is priced off whatever warrant row carries that
    stock's spot, so a plain share leg needs no extra fetch. Instruments with no
    current quote are simply absent from the result — the dashboard renders them
    as "no quote" rather than failing the whole request.
    """
    by_kind = {}
    for inst in instruments or []:
        kind, code = inst.get("kind"), inst.get("code")
        if not kind or not code:
            continue
        by_kind.setdefault(kind, []).append(inst)

    out, spots = {}, {}
    # An underlying leg needs only its stock's spot, which any warrant row on
    # that stock already carries — so it rides the warrant read, no extra fetch.
    share_codes = {(i.get("underlying_code") or i["code"])
                   for i in by_kind.get("underlying", [])}
    warrants = by_kind.get("warrant", [])
    if warrants or share_codes:
        q, s = _warrant_quotes(warrants, share_codes)
        out.update(q)
        spots.update(s)
    if by_kind.get("tw_option"):
        q, s = _tw_option_quotes(by_kind["tw_option"])
        out.update(q)
        for code, spot in s.items():
            spots.setdefault(code, spot)

    for inst in by_kind.get("underlying", []):
        spot = spots.get(inst.get("underlying_code") or inst["code"])
        if spot is None:
            continue
        out[_key("underlying", inst["code"])] = {
            "bid": spot, "ask": spot, "iv": None, "underlying_price": spot,
            "name": inst["code"], "underlying_code": inst["code"],
        }
    return out
