"""Alert delivery: both channels independent, unconfigured ones skipped, and a
failing webhook never raising into the worker that was reporting an outage.

requests.post is the faked boundary — no HTTP request is made.
"""
import pytest

from services.broker import alerts


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return _Resp()

    monkeypatch.setattr(alerts.requests, "post", fake_post)
    monkeypatch.delenv(alerts.SLACK_ENV, raising=False)
    monkeypatch.delenv(alerts.NTFY_ENV, raising=False)
    return calls


def test_both_channels_fire_when_both_are_configured(posts, monkeypatch):
    monkeypatch.setenv(alerts.SLACK_ENV, "https://hooks.slack.test/abc")
    monkeypatch.setenv(alerts.NTFY_ENV, "https://ntfy.test/topic")

    assert alerts.notify("feed down") == {"slack": True, "ntfy": True}
    assert [url for url, _kw in posts] == [
        "https://hooks.slack.test/abc", "https://ntfy.test/topic"]
    assert posts[0][1]["json"] == {"text": "feed down"}
    assert posts[1][1]["data"] == b"feed down"


def test_an_unconfigured_channel_is_skipped_not_attempted(posts, monkeypatch):
    monkeypatch.setenv(alerts.SLACK_ENV, "https://hooks.slack.test/abc")

    assert alerts.notify("feed down") == {"slack": True, "ntfy": False}
    assert len(posts) == 1


def test_no_channels_configured_is_silent(posts):
    assert alerts.notify("feed down") == {"slack": False, "ntfy": False}
    assert posts == []


def test_one_channel_failing_does_not_stop_the_other(monkeypatch):
    monkeypatch.setenv(alerts.SLACK_ENV, "https://hooks.slack.test/abc")
    monkeypatch.setenv(alerts.NTFY_ENV, "https://ntfy.test/topic")

    def fake_post(url, **kw):
        if "slack" in url:
            raise ConnectionError("slack unreachable")
        return _Resp()

    monkeypatch.setattr(alerts.requests, "post", fake_post)
    # The redundancy is the point: Slack being down must not mean no alert.
    assert alerts.notify("feed down") == {"slack": False, "ntfy": True}


def test_an_error_status_counts_as_undelivered(monkeypatch):
    monkeypatch.setenv(alerts.SLACK_ENV, "https://hooks.slack.test/abc")
    monkeypatch.delenv(alerts.NTFY_ENV, raising=False)
    monkeypatch.setattr(alerts.requests, "post", lambda url, **kw: _Resp(500))

    assert alerts.notify("feed down")["slack"] is False


def test_notify_never_raises(monkeypatch):
    monkeypatch.setenv(alerts.SLACK_ENV, "https://hooks.slack.test/abc")
    monkeypatch.setenv(alerts.NTFY_ENV, "https://ntfy.test/topic")
    monkeypatch.setattr(alerts.requests, "post",
                        lambda url, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    assert alerts.notify("feed down") == {"slack": False, "ntfy": False}
