"""Pure decision logic for the Live Options tab: contract-field parsing and
the add-only chain diff. No Flask context, no Supabase, no Fubon SDK — same
convention as logic/live_warrant_logic.py.

Connection-pool assignment and best-level collapsing are NOT duplicated
here — services/live_options.py imports assign_slot/best_level/MAX_*/
CapacityExceededError/check_capacity directly from logic/live_warrant_logic,
which are already generic (assign_slot takes a bare list of ints, best_level
takes bare bid/ask lists — neither assumes anything warrant-specific).

HIGH-RISK / UNVERIFIED: Fugle's documented `tickers()` response fields are
`symbol, type, name, referencePrice, contractType, startDate, endDate,
flowGroup, settlementDate, isDynamicBanding, isSpread` — no strike price and
no call/put field, for any contract type, options included. Every parse_*
below tries structured fields first (harmless if genuinely absent) but is
written assuming the symbol/name decode below is what actually fires. This
needs confirming against a live account (see services/live_options.py's
load_chain docstring for the one-shot diagnostic) before it's trusted.
"""
import re
from datetime import datetime

# Speculative key names for a structured field, tried before any fallback.
# expiryDate/maturityDate/deliveryDate are common option-API namings but
# undocumented for this endpoint; settlementDate/endDate ARE documented
# generic tickers() fields, included here in case one of them holds the
# contract's last-trading-day.
_EXPIRY_KEYS = ("expiryDate", "maturityDate", "settlementDate", "endDate", "deliveryDate")
_STRIKE_KEYS = ("strikePrice", "exercisePrice", "strike")
_CALLPUT_KEYS = ("callPut", "call_put", "right", "optionRight")

# TAIFEX/CME-style month-code letter: calls A-L (Jan-Dec), puts M-X (Jan-Dec).
# UNVERIFIED for single-stock option symbols specifically — the only place
# this shape is exercised today (scripts/fubon_quote_viewer.py's
# _OPTION_SYMBOL_RE) is against TXO (index option) symbols, and even there
# it's only ever used to extract the strike digits, never the month letter.
_SYMBOL_RE = re.compile(r"^([A-Z0-9]+?)(\d+)([A-Za-z])(\d)$")
_CALL_MONTHS = "ABCDEFGHIJKL"
_PUT_MONTHS = "MNOPQRSTUVWX"


def _parse_date_str(v):
    """"20261016" / "2026-10-16" / "2026/10/16" / an ISO datetime -> a date,
    or None for anything else. Defensive since the real field's format is
    unconfirmed."""
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_expiry(row):
    """A contract's expiry date, or None if no candidate field parses."""
    for key in _EXPIRY_KEYS:
        v = row.get(key)
        if v:
            d = _parse_date_str(v)
            if d:
                return d
    return None


def parse_strike(row):
    """A contract's strike price, or None. Structured field first, then the
    digit group in a compact symbol (e.g. "CDA06500L4" -> 6500)."""
    for key in _STRIKE_KEYS:
        v = row.get(key)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f

    m = _SYMBOL_RE.match(row.get("symbol") or "")
    if m:
        try:
            # Confirmed live against a 2330 (TSMC) chain, 2026-08-27: the
            # digit group carries an implied one-decimal-place strike (e.g.
            # "23500" -> 2350.0, "18600" -> 1860.0) — a bare 10x too high
            # without this division. TXO (index) symbols, the only shape
            # this fallback was validated against before, need no such
            # scaling since index strikes are already whole numbers.
            strike = float(m.group(2)) / 10
        except ValueError:
            return None
        if strike > 0:
            return strike
    return None


def parse_call_put(row):
    """True=put, False=call, None=undeterminable.

    Structured flag (only trusted as an explicit CALL/PUT/C/P string — a raw
    boolean under an unfamiliar key is too ambiguous to guess the polarity
    of) -> symbol month-letter -> name text (買權/賣權, the standard TAIFEX
    option terminology, or English CALL/PUT).
    """
    for key in _CALLPUT_KEYS:
        v = row.get(key)
        if v is None:
            continue
        s = str(v).strip().upper()
        if s in ("PUT", "P"):
            return True
        if s in ("CALL", "C"):
            return False

    m = _SYMBOL_RE.match(row.get("symbol") or "")
    if m:
        letter = m.group(3).upper()
        if letter in _CALL_MONTHS:
            return False
        if letter in _PUT_MONTHS:
            return True

    name = str(row.get("name") or "")
    if "買權" in name:
        return False
    if "賣權" in name:
        return True
    upper = name.upper()
    if "CALL" in upper:
        return False
    if "PUT" in upper:
        return True
    return None


def parse_contract(row):
    """One tickers() row -> {"code","expiry","strike","is_put","name"}, or
    None if code/expiry/strike/is_put isn't resolvable — the caller skips
    rather than half-tracking a contract it can't place in the chain grid."""
    code = row.get("symbol")
    if not code:
        return None
    expiry = parse_expiry(row)
    strike = parse_strike(row)
    is_put = parse_call_put(row)
    if expiry is None or strike is None or is_put is None:
        return None
    return {
        "code": code,
        "expiry": expiry,
        "strike": strike,
        "is_put": is_put,
        "name": row.get("name") or code,
    }


def new_contract_codes(tracked_codes, parsed_codes):
    """Codes to subscribe: parsed minus already-tracked, deduped. Add-only —
    see services/live_options.py::load_chain's docstring for why no
    ChainShrinkError-style removal guard is needed here."""
    tracked = set(tracked_codes)
    seen = set()
    out = []
    for code in parsed_codes:
        if code in tracked or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out
