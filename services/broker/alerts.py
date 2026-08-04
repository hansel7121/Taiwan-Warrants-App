"""Disconnect notifications: Slack Incoming Webhook + ntfy.sh push.

Two channels fire independently and redundantly on purpose (docs/adr/0004): a
dropped feed during trading hours is exactly the event where Slack being down,
or a phone not receiving push, must not mean no alert at all.

Nothing here ever raises. An alert is a side channel — a webhook timing out
must not take down the worker whose outage it was reporting. Every failure is
logged and swallowed, and a channel with no env var configured is simply not a
channel (silently skipped, so a local run needs no webhook set up).

WHEN to alert is not decided here: resilience.py owns the grace threshold and
the once-per-incident gate.
"""
import logging
import os

import requests


SLACK_ENV = "SLACK_WEBHOOK_URL"
NTFY_ENV = "NTFY_TOPIC_URL"

TIMEOUT_SEC = 10

log = logging.getLogger("broker_worker.alerts")


def notify(text, title="Broker feed disconnected"):
    """Fire both channels. Returns {channel: delivered?} for logging/tests."""
    return {"slack": _post_slack(text), "ntfy": _post_ntfy(text, title)}


def _post_slack(text):
    url = os.environ.get(SLACK_ENV)
    if not url:
        return False
    return _send(lambda: requests.post(url, json={"text": text}, timeout=TIMEOUT_SEC),
                 "slack")


def _post_ntfy(text, title):
    url = os.environ.get(NTFY_ENV)
    if not url:
        return False
    # ntfy takes the body as raw text and everything else as headers.
    return _send(
        lambda: requests.post(
            url,
            data=text.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "warning"},
            timeout=TIMEOUT_SEC,
        ),
        "ntfy",
    )


def _send(call, channel):
    try:
        resp = call()
    except Exception as e:
        log.warning("alert channel %s failed: %s: %s", channel, type(e).__name__, e)
        return False
    if resp.status_code >= 400:
        log.warning("alert channel %s returned %s", channel, resp.status_code)
        return False
    return True
