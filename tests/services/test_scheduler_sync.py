"""sync_tw_option / sync_us_option tests — the shared _sync_option_products loop.

Real in-process calls; only the true I/O boundaries are monkeypatched: the two
scrapers, the tracked-product lists (Supabase reads), and db_market.write_snapshot
(a real Supabase write we must never perform from a test).
"""
import pandas as pd
import pytest

from services import scheduler


def _tw_rows(code, n=2):
    """A minimal TW option frame, shaped like scrape_tw_option's output."""
    return pd.DataFrame({
        "contract": [f"{code}-C{i}" for i in range(n)],
        "type": ["Call"] * n,
        "strike": [100.0 + i for i in range(n)],
        "days_to_expiry": [30.0] * n,
        "bid": [1.0] * n,
        "ask": [1.2] * n,
        "volume": [5.0] * n,
        "oi": [7.0] * n,
    })


def _us_rows(code, n=2):
    """A minimal US ADR option frame, shaped like scrape_yfinance_us_option's."""
    return pd.DataFrame({
        "contract": [f"{code}-USC{i}" for i in range(n)],
        "type": ["Call"] * n,
        "strike": [50.0 + i for i in range(n)],
        "days_to_expiry": [45.0] * n,
        "bid": [2.0] * n,
        "ask": [2.3] * n,
        "volume": [3.0] * n,
        "oi": [9.0] * n,
    })


@pytest.fixture
def writes(monkeypatch):
    """Capture db_market.write_snapshot calls instead of hitting Supabase."""
    calls = []
    monkeypatch.setattr(
        scheduler.db_market, "write_snapshot",
        lambda name, df: calls.append((name, df)),
    )
    return calls


def test_sync_tw_option_survives_one_dead_product(monkeypatch, writes):
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330", "2303", "9999"])

    def fake_scrape(codes, opt_type, min_days=0, max_days=365, **kw):
        code = codes[0]
        assert codes == [code], "helper must pass the scraper a single-code list"
        if code == "9999":
            raise RuntimeError("boom")
        return _tw_rows(code), None, {}

    monkeypatch.setattr(scheduler.options_logic, "scrape_tw_option", fake_scrape)

    scheduler.sync_tw_option()

    assert len(writes) == 1
    name, df = writes[0]
    assert name == "tw_options"
    assert sorted(df["stock_code"].unique()) == ["2303", "2330"]
    assert len(df) == 4
    # TW-only provenance column: present, all null.
    assert "source" in df.columns
    assert df["source"].isna().all()
    # Aligned to the schema's column order, and integer columns coerced.
    assert list(df.columns) == scheduler.TW_OPTION_COLS
    assert str(df["volume"].dtype) == "Int64"


def test_sync_us_option_survives_one_dead_product(monkeypatch, writes):
    monkeypatch.setattr(scheduler, "us_option_codes", lambda: ["2330", "2303", "9999"])

    def fake_scrape(code, opt_type, min_days=1, max_days=730, **kw):
        if code == "9999":
            raise RuntimeError("boom")
        return _us_rows(code), None, {}

    monkeypatch.setattr(
        scheduler.us_options_logic, "scrape_yfinance_us_option", fake_scrape)

    scheduler.sync_us_option()

    assert len(writes) == 1
    name, df = writes[0]
    assert name == "us_options"
    assert sorted(df["stock_code"].unique()) == ["2303", "2330"]
    assert len(df) == 4
    # The TW-only source column must NOT leak into the US path.
    assert "source" not in df.columns
    assert list(df.columns) == scheduler.US_OPTION_COLS
    assert str(df["oi"].dtype) == "Int64"


def test_sync_tw_option_skips_write_when_all_codes_empty(monkeypatch, writes, capsys):
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330", "2303"])
    monkeypatch.setattr(
        scheduler.options_logic, "scrape_tw_option",
        lambda codes, opt_type, **kw: (pd.DataFrame(), "no data", {}),
    )

    scheduler.sync_tw_option()

    assert writes == []
    assert "SCHED: tw_options empty, skipping write" in capsys.readouterr().out


def test_sync_us_option_skips_write_when_all_codes_empty(monkeypatch, writes, capsys):
    monkeypatch.setattr(scheduler, "us_option_codes", lambda: ["2330", "2303"])
    monkeypatch.setattr(
        scheduler.us_options_logic, "scrape_yfinance_us_option",
        lambda code, opt_type, **kw: (None, "no data", {}),
    )

    scheduler.sync_us_option()

    assert writes == []
    assert "SCHED: us_options empty, skipping write" in capsys.readouterr().out


def test_per_code_failure_and_empty_log_lines_are_unchanged(monkeypatch, writes, capsys):
    monkeypatch.setattr(scheduler, "tw_option_codes", lambda: ["2330", "2303", "9999"])

    def fake_scrape(codes, opt_type, **kw):
        code = codes[0]
        if code == "9999":
            raise RuntimeError("boom")
        if code == "2303":
            return pd.DataFrame(), "no rows", {}
        return _tw_rows(code), None, {}

    monkeypatch.setattr(scheduler.options_logic, "scrape_tw_option", fake_scrape)

    scheduler.sync_tw_option()

    out = capsys.readouterr().out
    assert "SCHED: tw_option 9999 failed: boom" in out
    assert "SCHED: tw_option 2303 empty (no rows)" in out
    assert len(writes) == 1
    assert sorted(writes[0][1]["stock_code"].unique()) == ["2330"]
