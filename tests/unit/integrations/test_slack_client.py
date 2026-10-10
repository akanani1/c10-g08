"""Tests for incident_suite/integrations/slack_client.py, against httpx.MockTransport."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from incident_suite.config import AppConfig
from incident_suite.integrations.slack_client import SlackClient
from incident_suite.state import NotificationPlan, SlackOutcome

PLAN = NotificationPlan(
    post=True,
    reason="At least one medium or worse issue.",
    summary="[HIGH] 1 issue(s) in app.log (run run-1)\n- ISS-1 something",
    thread="Do now:\n- Roll back (ISS-1)",
)
LINK = "https://team.slack.com/archives/C123/p1700000000000100"
POSTED = httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "1700000000.000100"})
REPLIED = httpx.Response(200, json={"ok": True, "ts": "1700000001.000200"})
PERMALINK = httpx.Response(200, json={"ok": True, "channel": "C123", "permalink": LINK})

Reply = httpx.Response | Exception
MakeClient = Callable[..., tuple[SlackClient, list[httpx.Request], list[float]]]


@pytest.fixture
def make_client() -> MakeClient:
    """``client, requests, waits = make_client(reply, ...)``: replies are used in order."""
    config = AppConfig(slack_bot_token=SecretStr("xoxb-test-token"), slack_channel_id="C123")

    def make(*replies: Reply) -> tuple[SlackClient, list[httpx.Request], list[float]]:
        queue = list(replies)
        requests: list[httpx.Request] = []
        waits: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client = SlackClient(config, transport=httpx.MockTransport(handler), sleep=waits.append)
        return client, requests, waits

    return make


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def test_needs_complete_credentials() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        SlackClient(AppConfig(slack_bot_token=SecretStr("xoxb-test-token")))


def test_post_summary_thread_and_permalink(make_client: MakeClient) -> None:
    client, requests, _ = make_client(POSTED, REPLIED, PERMALINK)
    outcome = client.post(PLAN, [])

    assert outcome == SlackOutcome(sent=True, permalink=LINK)
    summary, thread, permalink = requests
    assert summary.url.path == "/api/chat.postMessage"
    assert summary.headers["Authorization"] == "Bearer xoxb-test-token"
    assert summary.headers["Content-Type"] == "application/json; charset=utf-8"
    assert _body(summary)["channel"] == "C123"
    assert _body(summary)["unfurl_links"] is False
    assert "thread_ts" not in _body(summary)
    assert _body(thread)["thread_ts"] == "1700000000.000100"
    assert permalink.method == "GET"
    assert permalink.url.path == "/api/chat.getPermalink"
    assert dict(permalink.url.params) == {"channel": "C123", "message_ts": "1700000000.000100"}


def test_empty_thread_is_not_posted(make_client: MakeClient) -> None:
    client, requests, _ = make_client(POSTED, PERMALINK)
    assert client.post(PLAN.model_copy(update={"thread": ""}), []).sent
    assert len(requests) == 2


def test_summary_failure_stops_everything(make_client: MakeClient) -> None:
    client, requests, _ = make_client(
        httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    outcome = client.post(PLAN, [])
    assert outcome == SlackOutcome(error="Slack chat.postMessage failed: channel_not_found")
    assert len(requests) == 1


def test_later_failures_still_count_as_sent(make_client: MakeClient) -> None:
    client, _, _ = make_client(
        POSTED,
        httpx.Response(200, json={"ok": False, "error": "msg_too_long"}),
        httpx.Response(200, json={"ok": True, "permalink": "javascript:alert(1)"}),
    )
    outcome = client.post(PLAN, [])
    assert outcome.sent
    assert outcome.permalink is None
    assert outcome.error == (
        "Slack chat.postMessage failed: msg_too_long; Slack chat.getPermalink didn't return a link"
    )


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (httpx.Response(200, json={"ok": True}), "didn't return the message ts"),
        (httpx.Response(200, text="<html>"), "isn't JSON"),
        (httpx.Response(200, json=["odd"]), "chat.postMessage failed: "),
        (httpx.Response(404), "returned HTTP 404"),
        (httpx.ReadTimeout("slow"), r"couldn't be reached \(ReadTimeout\)"),
    ],
)
def test_odd_answers(make_client: MakeClient, reply: Reply, error: str) -> None:
    client, _, _ = make_client(reply)
    outcome = client.post(PLAN, [])
    assert not outcome.sent
    assert outcome.error is not None
    assert re.search(error, outcome.error)
    assert "xoxb" not in outcome.error


def test_rate_limit_waits_for_retry_after(make_client: MakeClient) -> None:
    client, requests, waits = make_client(
        httpx.Response(429, headers={"Retry-After": "3"}), POSTED, REPLIED, PERMALINK
    )
    assert client.post(PLAN, []).sent
    assert len(requests) == 4
    assert waits == [3.0]
    client.close()
