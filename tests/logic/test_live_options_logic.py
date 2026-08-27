"""Live Options tab decision logic: contract-field parsing (structured field
-> symbol decode -> name decode fallback chain) and the add-only chain diff.

Every test here is offline — no SDK, no live account. This is what proves
the parser is internally consistent; it does NOT prove it matches Fubon's
real tickers() response shape, which is unverified (see the module
docstring in logic/live_options_logic.py and the Phase 1.5 live diagnostic
in services/live_options.py::load_chain).
"""
import datetime as dt

from logic import live_options_logic as lol


# ── parse_expiry ─────────────────────────────────────────────────────────

def test_parse_expiry_yyyymmdd():
    assert lol.parse_expiry({"expiryDate": "20261016"}) == dt.date(2026, 10, 16)


def test_parse_expiry_iso_dashed():
    assert lol.parse_expiry({"maturityDate": "2026-10-16"}) == dt.date(2026, 10, 16)


def test_parse_expiry_slashed():
    assert lol.parse_expiry({"settlementDate": "2026/10/16"}) == dt.date(2026, 10, 16)


def test_parse_expiry_iso_datetime():
    assert lol.parse_expiry({"endDate": "2026-10-16T00:00:00"}) == dt.date(2026, 10, 16)


def test_parse_expiry_prefers_first_matching_key():
    row = {"expiryDate": "20261016", "maturityDate": "20261231"}
    assert lol.parse_expiry(row) == dt.date(2026, 10, 16)


def test_parse_expiry_none_when_absent():
    assert lol.parse_expiry({"symbol": "CDA06500L4"}) is None


def test_parse_expiry_none_when_unparsable():
    assert lol.parse_expiry({"expiryDate": "not-a-date"}) is None


# ── parse_strike ─────────────────────────────────────────────────────────

def test_parse_strike_structured_field():
    assert lol.parse_strike({"strikePrice": 650.0}) == 650.0


def test_parse_strike_structured_field_string():
    assert lol.parse_strike({"exercisePrice": "650"}) == 650.0


def test_parse_strike_falls_back_to_symbol_digits():
    """"06500" carries an implied one-decimal-place strike, confirmed live
    against a real 2330 chain (see parse_strike's comment) -> 650.0, not 6500."""
    assert lol.parse_strike({"symbol": "CDA06500L4"}) == 650.0


def test_parse_strike_zero_structured_value_falls_through_to_symbol():
    assert lol.parse_strike({"strike": 0, "symbol": "CDA06500L4"}) == 650.0


def test_parse_strike_none_when_nothing_matches():
    assert lol.parse_strike({"symbol": "not-a-contract-code"}) is None


# ── parse_call_put ───────────────────────────────────────────────────────

def test_parse_call_put_structured_call_text():
    assert lol.parse_call_put({"callPut": "CALL"}) is False


def test_parse_call_put_structured_put_text():
    assert lol.parse_call_put({"right": "P"}) is True


def test_parse_call_put_structured_field_ignores_ambiguous_bool():
    """A raw boolean under an unfamiliar key isn't trusted — too ambiguous
    to guess which polarity True means — so this falls through to the next
    layer instead of guessing wrong."""
    row = {"call_put": True, "symbol": "CDA06500M4"}  # M = put month-letter
    assert lol.parse_call_put(row) is True


def test_parse_call_put_symbol_call_month_letter():
    assert lol.parse_call_put({"symbol": "CDA06500A4"}) is False  # A = call


def test_parse_call_put_symbol_put_month_letter():
    assert lol.parse_call_put({"symbol": "CDA06500M4"}) is True  # M = put


def test_parse_call_put_name_chinese_call():
    assert lol.parse_call_put({"symbol": "???", "name": "台積電1月買權06500"}) is False


def test_parse_call_put_name_chinese_put():
    assert lol.parse_call_put({"symbol": "???", "name": "台積電1月賣權06500"}) is True


def test_parse_call_put_name_english():
    assert lol.parse_call_put({"symbol": "???", "name": "TSMC PUT 650"}) is True


def test_parse_call_put_none_when_undeterminable():
    assert lol.parse_call_put({"symbol": "???", "name": "unlabeled"}) is None


# ── parse_contract ───────────────────────────────────────────────────────

def test_parse_contract_full_row_structured():
    row = {
        "symbol": "CDA06500A4",
        "name": "台積電1月買權06500",
        "expiryDate": "20260116",
        "strikePrice": 650.0,
        "callPut": "CALL",
    }
    assert lol.parse_contract(row) == {
        "code": "CDA06500A4", "expiry": dt.date(2026, 1, 16),
        "strike": 650.0, "is_put": False, "name": "台積電1月買權06500",
    }


def test_parse_contract_full_row_all_fallback():
    """No structured fields at all — expiry still can't be recovered without
    one, so the row is skipped even though strike/call-put decode fine."""
    row = {"symbol": "CDA06500M4", "name": "台積電1月賣權06500"}
    assert lol.parse_contract(row) is None


def test_parse_contract_missing_symbol_is_none():
    assert lol.parse_contract({"expiryDate": "20260116", "strikePrice": 650.0}) is None


def test_parse_contract_missing_expiry_is_none():
    row = {"symbol": "CDA06500A4", "strikePrice": 650.0, "callPut": "CALL"}
    assert lol.parse_contract(row) is None


def test_parse_contract_missing_strike_is_none():
    row = {"symbol": "not-a-contract-code", "expiryDate": "20260116", "callPut": "CALL"}
    assert lol.parse_contract(row) is None


def test_parse_contract_missing_call_put_is_none():
    row = {"symbol": "???", "expiryDate": "20260116", "strikePrice": 650.0}
    assert lol.parse_contract(row) is None


def test_parse_contract_name_falls_back_to_code_when_absent():
    row = {
        "symbol": "CDA06500A4", "expiryDate": "20260116",
        "strikePrice": 650.0, "callPut": "CALL",
    }
    assert lol.parse_contract(row)["name"] == "CDA06500A4"


# ── new_contract_codes ───────────────────────────────────────────────────

def test_new_contract_codes_excludes_already_tracked():
    assert lol.new_contract_codes(["A", "B"], ["A", "C"]) == ["C"]


def test_new_contract_codes_dedupes_within_parsed():
    assert lol.new_contract_codes([], ["A", "A", "B"]) == ["A", "B"]


def test_new_contract_codes_preserves_order():
    assert lol.new_contract_codes([], ["C", "A", "B"]) == ["C", "A", "B"]


def test_new_contract_codes_empty_when_everything_already_tracked():
    assert lol.new_contract_codes(["A", "B"], ["A", "B"]) == []
