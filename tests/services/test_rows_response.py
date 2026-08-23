"""The rows+count response envelope must stay byte-identical to the old encoding.

`_rows_json` replaced `jsonify({"rows": json.loads(df.to_json(...)), ...})` on the
eight data/arb routes. The `rows` array is the contract the frontend parses, so it
is asserted byte-for-byte against pandas' own output — NaN placement, nested leg
dicts, unicode and float precision included. Only the envelope's key ORDER may
differ (jsonify sorted keys; the splice puts `rows` first).
"""
import json
import os

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("LOCAL_USER_ID", "user-a")
os.environ["APP_ENV"] = "local"

import app as app_module  # noqa: E402
from logic import warrant_logic  # noqa: E402

FRAME = pd.DataFrame([
    {"warrant_code": "031100", "warrant_name": "台積電購01", "type": "Call",
     "underlying_price": 612.0, "ask": 2.1, "bid": np.nan, "days_to_expiry": 90,
     "strike": 600.0, "exercise_ratio": 0.5, "iv_ask": 0.3123456789,
     "volume": 1200, "riskless": True},
    {"warrant_code": "031101", "warrant_name": "UMC C2", "type": "Put",
     "underlying_price": 48.5, "ask": np.nan, "bid": 0.44, "days_to_expiry": 12,
     "strike": 50.0, "exercise_ratio": 1.0, "iv_ask": np.nan,
     "volume": 0, "riskless": False},
])

# match_static_arb is the one frame carrying nested per-leg dicts.
NESTED = pd.DataFrame([
    {"iv_edge_pts": 4.2,
     "long_call": {"source": "warrant", "id": "031100", "K": 600.0, "dte": 90, "iv": 0.31},
     "short_put": None},
])


def _body(df, **extra):
    """Run `_rows_json` inside a request context and return the decoded response."""
    with app_module.app.test_request_context("/read_warrant", method="POST"):
        resp = app_module._rows_json(df, **extra)
    assert resp.mimetype == "application/json"
    return resp.get_data(as_text=True)


def _expected_rows(df):
    return df.to_json(orient="records") if not df.empty else "[]"


@pytest.mark.parametrize("df", [FRAME, NESTED, pd.DataFrame()])
def test_rows_array_is_pandas_output_verbatim(df):
    body = _body(df)
    assert body.startswith('{"rows":' + _expected_rows(df) + ",")
    assert json.loads(body)["rows"] == json.loads(_expected_rows(df))


def test_count_and_extras_present():
    payload = json.loads(_body(FRAME, as_of="2026-08-22T05:00:00+00:00", source="live"))
    assert payload["count"] == 2
    assert payload["as_of"] == "2026-08-22T05:00:00+00:00"
    assert payload["source"] == "live"


def test_matches_the_old_jsonify_payload():
    """Same object graph as `jsonify({"rows": json.loads(df.to_json(...)), ...})`."""
    old = {"rows": json.loads(FRAME.to_json(orient="records")), "count": len(FRAME),
           "as_of": None}
    assert json.loads(_body(FRAME, as_of=None)) == old


def test_nan_becomes_null_not_a_bare_nan_literal():
    body = _body(FRAME)
    assert "NaN" not in body            # JSON.parse would reject a bare NaN
    rows = json.loads(body)["rows"]
    assert rows[0]["bid"] is None and rows[1]["iv_ask"] is None


def test_empty_frame_gives_an_empty_rows_array():
    payload = json.loads(_body(pd.DataFrame()))
    assert payload == {"rows": [], "count": 0}


def test_read_warrant_route_returns_the_spliced_body(monkeypatch):
    monkeypatch.setattr(
        warrant_logic, "read_warrant",
        lambda *a, **k: (FRAME, None, {"source": "live"}),
    )
    client = app_module.app.test_client()
    resp = client.post("/read_warrant", json={"stock_codes": ["2330"]})
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    payload = resp.get_json()
    assert payload["count"] == 2
    assert payload["source"] == "live"
    assert payload["rows"] == json.loads(FRAME.to_json(orient="records"))
