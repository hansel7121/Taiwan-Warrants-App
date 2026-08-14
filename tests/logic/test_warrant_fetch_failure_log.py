"""What the warrant fetcher says when one underlying comes back empty.

The failure line is the only signal that a stock's warrants are unfetchable, so
it has to name the stock and size the failure against that stock's own code
count. It previously divided by the whole batch, which made a 3-warrant stock
read as a 5793-code outage.
"""
import logic.warrant_logic as W


def _capture(monkeypatch):
    """Collect applog lines instead of printing them."""
    lines = []
    monkeypatch.setattr(W.applog, "log",
                        lambda tag, msg, level="INFO", **kw: lines.append((level, msg)))
    return lines


def _run(monkeypatch, code_map, fetched):
    """Drive get_warrant_results with CMoney and the universe stubbed out."""
    monkeypatch.setattr(W, "_warrant_codes_for", lambda stocks: code_map)
    monkeypatch.setattr(W, "get_cmoney_prices",
                        lambda codes, errors_out=None: {c: {"Warrant": {}, "Stock": {}}
                                                        for c in codes if c in fetched})
    monkeypatch.setattr(W, "build_warrant_df", lambda *a, **k: None)
    W._warrant_cache.invalidate()
    W.get_warrant_results(list(code_map), force=True)


def test_failure_line_counts_against_that_stocks_own_codes(monkeypatch):
    lines = _capture(monkeypatch)
    _run(monkeypatch,
         {"2382": ["073459", "073461", "073462"], "2330": ["a%d" % i for i in range(50)]},
         fetched={"a%d" % i for i in range(50)})          # every 2382 code fails

    errors = [m for lvl, m in lines if lvl == "ERROR"]
    assert len(errors) == 1, errors
    assert "2382 (0/3 codes ok)" in errors[0], errors[0]
    assert "0/53" not in errors[0]                        # not the batch total
    assert "2330" not in errors[0]                        # the healthy stock is not named


def test_each_failed_stock_is_sized_separately(monkeypatch):
    lines = _capture(monkeypatch)
    _run(monkeypatch, {"2382": ["x1", "x2", "x3"], "9999": ["y1"]}, fetched=set())

    err = [m for lvl, m in lines if lvl == "ERROR"][0]
    assert "2382 (0/3 codes ok)" in err and "9999 (0/1 codes ok)" in err


def test_no_error_when_every_stock_returns_something(monkeypatch):
    lines = _capture(monkeypatch)
    _run(monkeypatch, {"2330": ["a1", "a2"]}, fetched={"a1", "a2"})
    assert [m for lvl, m in lines if lvl == "ERROR"] == []


def test_a_stock_with_no_warrants_is_not_a_failure(monkeypatch):
    """No codes at all is "this stock has no warrants", not a fetch failure."""
    lines = _capture(monkeypatch)
    _run(monkeypatch, {"2382": []}, fetched=set())
    assert [m for lvl, m in lines if lvl == "ERROR"] == []
