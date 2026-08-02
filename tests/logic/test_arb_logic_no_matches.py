"""The orchestrators' empty-scan vs hard-failure split.

A scan that reads every code fine and simply matches nothing is a normal
outcome and raises NoMatchesError; a scan where a data source actually failed
raises a plain RuntimeError, so a narrow `except NoMatchesError` caller (the
scheduler's sync_suggestions) can never swallow a genuine failure.

Only the true I/O boundaries are patched — the read_* fetchers and the product
maps. arb_logic itself runs for real.
"""
import pandas as pd
import pytest

from logic import arb_logic
from logic import options_logic
from logic import us_options_logic
from logic import warrant_logic
from services import applog

CODE = "2330"

EMPTY = (pd.DataFrame(), None, {})
SOFT_EMPTY = (pd.DataFrame(), "no data", {})
HARD_FAIL = (pd.DataFrame(), "CMoney unreachable", {"hard_error": True})


def _fixed(result):
    """A fetcher stub that ignores its args and returns `result`."""
    return lambda *a, **k: result


@pytest.fixture
def stub_maps(monkeypatch):
    """Make CODE a known product on both the TW-option and US-ADR side, so the
    orchestrators get past their mapping guards and reach the fetchers."""
    monkeypatch.setattr(options_logic, "_commodity_map",
                        lambda: {CODE: {"exercise_ratio": 2000}})
    monkeypatch.setattr(us_options_logic, "_adr_map", lambda: {CODE: {"symbol": "TSM"}})
    monkeypatch.setattr(us_options_logic, "contract_tw_shares", lambda code: 500)


# ── match_warrant_tw_option ──────────────────────────────────────────────────

def test_warrant_tw_clean_empty_scan_raises_no_matches(stub_maps, monkeypatch):
    monkeypatch.setattr(warrant_logic, "read_warrant", _fixed(SOFT_EMPTY))
    monkeypatch.setattr(options_logic, "read_tw_option", _fixed(SOFT_EMPTY))

    with pytest.raises(arb_logic.NoMatchesError) as exc:
        arb_logic.match_warrant_tw_option([CODE], "All", 3.0, 5)

    assert type(exc.value) is arb_logic.NoMatchesError


def test_warrant_tw_hard_error_raises_plain_runtime_error(stub_maps, monkeypatch):
    monkeypatch.setattr(warrant_logic, "read_warrant", _fixed(HARD_FAIL))
    monkeypatch.setattr(options_logic, "read_tw_option", _fixed(EMPTY))

    with pytest.raises(RuntimeError) as exc:
        arb_logic.match_warrant_tw_option([CODE], "All", 3.0, 5)

    assert not isinstance(exc.value, arb_logic.NoMatchesError)
    assert "CMoney unreachable" in str(exc.value)


# ── match_warrant_us_option ──────────────────────────────────────────────────

def test_warrant_us_clean_empty_scan_raises_no_matches(stub_maps, monkeypatch):
    monkeypatch.setattr(warrant_logic, "read_warrant", _fixed(SOFT_EMPTY))
    monkeypatch.setattr(us_options_logic, "read_us_option", _fixed(SOFT_EMPTY))

    with pytest.raises(arb_logic.NoMatchesError) as exc:
        arb_logic.match_warrant_us_option([CODE], "All", 3.0, 5)

    assert type(exc.value) is arb_logic.NoMatchesError


def test_warrant_us_hard_error_raises_plain_runtime_error(stub_maps, monkeypatch):
    monkeypatch.setattr(warrant_logic, "read_warrant", _fixed(HARD_FAIL))
    monkeypatch.setattr(us_options_logic, "read_us_option", _fixed(EMPTY))

    with pytest.raises(RuntimeError) as exc:
        arb_logic.match_warrant_us_option([CODE], "All", 3.0, 5)

    assert not isinstance(exc.value, arb_logic.NoMatchesError)
    assert "CMoney unreachable" in str(exc.value)


# ── match_tw_us_option ───────────────────────────────────────────────────────

def test_tw_us_clean_empty_scan_raises_no_matches(stub_maps, monkeypatch):
    monkeypatch.setattr(options_logic, "read_tw_option", _fixed(SOFT_EMPTY))
    monkeypatch.setattr(us_options_logic, "read_us_option", _fixed(SOFT_EMPTY))

    with pytest.raises(arb_logic.NoMatchesError) as exc:
        arb_logic.match_tw_us_option([CODE], "All", 3.0, 5)

    assert type(exc.value) is arb_logic.NoMatchesError


def test_tw_us_hard_error_raises_plain_runtime_error(stub_maps, monkeypatch):
    monkeypatch.setattr(options_logic, "read_tw_option",
                        _fixed((pd.DataFrame(), "TAIFEX unreachable", {"hard_error": True})))
    monkeypatch.setattr(us_options_logic, "read_us_option", _fixed(EMPTY))

    with pytest.raises(RuntimeError) as exc:
        arb_logic.match_tw_us_option([CODE], "All", 3.0, 5)

    assert not isinstance(exc.value, arb_logic.NoMatchesError)
    assert "TAIFEX unreachable" in str(exc.value)


# ── the scheduler's narrowed catch ───────────────────────────────────────────

def test_no_matches_error_is_a_runtime_error_subclass():
    """Existing broad `except RuntimeError` callers keep working."""
    assert issubclass(arb_logic.NoMatchesError, RuntimeError)


# ── the shared empty-scan tail, called directly ──────────────────────────────
# All three orchestrators end their per-code loop in _finish_empty_scan; these
# pin its behavior without going through a fetcher.

def test_finish_empty_scan_hard_errors_win_over_skips():
    """A real data-source failure outranks any soft skips collected alongside."""
    with pytest.raises(RuntimeError) as exc:
        arb_logic._finish_empty_scan(["src A down", "src B down"], ["2330: no data"])

    assert not isinstance(exc.value, arb_logic.NoMatchesError)
    assert str(exc.value) == "src A down; src B down"


def test_finish_empty_scan_skips_only_raises_no_matches_with_reasons():
    with pytest.raises(arb_logic.NoMatchesError) as exc:
        arb_logic._finish_empty_scan([], ["2330: no data", "2303: no data"])

    assert str(exc.value) == "no matches; 2330: no data; 2303: no data"


def test_finish_empty_scan_bare_raises_no_matches_without_suffix():
    with pytest.raises(arb_logic.NoMatchesError) as exc:
        arb_logic._finish_empty_scan([], [])

    assert str(exc.value) == "no matches"


def test_finish_empty_scan_logs_skip_reasons_once(monkeypatch):
    logged = []
    monkeypatch.setattr(applog, "log", lambda *a: logged.append(a))

    with pytest.raises(arb_logic.NoMatchesError):
        arb_logic._finish_empty_scan([], ["2330: no data"])

    assert logged == [("ARB", "no matches; 2330: no data")]


def test_finish_empty_scan_does_not_log_when_no_skip_reasons(monkeypatch):
    logged = []
    monkeypatch.setattr(applog, "log", lambda *a: logged.append(a))

    with pytest.raises(arb_logic.NoMatchesError):
        arb_logic._finish_empty_scan([], [])

    assert logged == []
