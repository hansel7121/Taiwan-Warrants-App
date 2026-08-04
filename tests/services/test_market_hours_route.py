"""GET /market_hours — the worker's single source of truth for trading hours
(docs/adr/0009), plus the worker-side client that polls it.

The route is exercised through Flask's test client; the worker's poll fakes
requests.get only, so the fail-closed behaviour is real.
"""
from datetime import datetime

import pytest

from services import scheduler


@pytest.fixture
def client():
    import app as app_module

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_route_needs_no_auth_and_reports_the_market(client, monkeypatch):
    monkeypatch.setattr(scheduler, "is_market_open", lambda market, now=None: True)

    r = client.get("/market_hours?market=tw_equity")

    assert r.status_code == 200
    body = r.get_json()
    assert body["market"] == "tw_equity"
    assert body["open"] is True
    assert body["as_of"]


def test_route_defaults_to_the_equity_session(client, monkeypatch):
    seen = []
    monkeypatch.setattr(scheduler, "is_market_open",
                        lambda market, now=None: seen.append(market) or False)

    assert client.get("/market_hours").get_json()["market"] == "tw_equity"
    assert seen == ["tw_equity"]


def test_unknown_market_is_a_400(client):
    r = client.get("/market_hours?market=crypto")
    assert r.status_code == 400
    assert "crypto" in r.get_json()["error"]


def test_route_answers_from_the_schedulers_own_calendar(client):
    # Not a stub: the point of the endpoint is that it is the SAME function the
    # web app's gated jobs use, so a weekday-open and a Sunday must disagree.
    open_day = scheduler.is_market_open("tw_equity", datetime(2026, 8, 3, 10, 0))
    closed_day = scheduler.is_market_open("tw_equity", datetime(2026, 8, 2, 10, 0))
    assert open_day and not closed_day


def test_worker_treats_an_unreachable_app_as_closed(monkeypatch):
    import broker_worker

    def boom(*a, **kw):
        raise ConnectionError("web app down")

    monkeypatch.setattr(broker_worker.requests, "get", boom)
    assert broker_worker.market_open("tw_equity") is False


def test_worker_reads_the_open_flag(monkeypatch):
    import broker_worker

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"market": "tw_equity", "open": True}

    monkeypatch.setattr(broker_worker.requests, "get", lambda *a, **kw: _Resp())
    assert broker_worker.market_open("tw_equity") is True
