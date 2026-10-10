"""Slack Web API client with a bot token. Implements the agent's :class:`Messenger`.

One run makes three calls (section 6.4 of the plan):

1. ``chat.postMessage``: the summary in the channel. Its ``ts`` identifies the message.
2. ``chat.postMessage`` with ``thread_ts``: the checklist and fixes as a thread reply.
3. ``chat.getPermalink``: the link to the summary, which goes into the JIRA tickets.

Slack answers most errors with HTTP 200 and ``{"ok": false, "error": "<code>"}``, so both the
status and ``ok`` are checked. Only the error code is shown, never the token.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import httpx

from incident_suite.config import AppConfig
from incident_suite.integrations import TIMEOUT, Sleep, send_with_retry
from incident_suite.integrations.payloads import slack_summary, slack_thread
from incident_suite.state import NotificationPlan, SlackOutcome, TicketOutcome

API_URL = "https://slack.com/api/"
_MAX_ERROR_CHARS = 100


class SlackCallError(RuntimeError):
    """A Slack call failed. The message is safe to show."""


class SlackClient:
    """Posts to one channel as the app's bot user."""

    def __init__(
        self,
        config: AppConfig,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        token, channel = config.slack_bot_token, config.slack_channel_id
        if token is None or not channel:
            msg = "Slack credentials are incomplete"
            raise ValueError(msg)
        self._channel = channel
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            timeout=TIMEOUT,
            transport=transport,
        )

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        http_method = "GET" if "params" in kwargs else "POST"
        if http_method == "POST":
            kwargs["headers"] = {"Content-Type": "application/json; charset=utf-8"}
        try:
            response = send_with_retry(self._http, http_method, method, self._sleep, **kwargs)
        except httpx.HTTPError as exc:
            msg = f"Slack couldn't be reached ({type(exc).__name__})"
            raise SlackCallError(msg) from exc
        if response.is_error:
            msg = f"Slack {method} returned HTTP {response.status_code}"
            raise SlackCallError(msg)
        try:
            body = response.json()
        except ValueError as exc:
            msg = f"Slack {method} sent an answer that isn't JSON"
            raise SlackCallError(msg) from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            code = str(body.get("error", "unknown_error")) if isinstance(body, dict) else ""
            msg = f"Slack {method} failed: {code[:_MAX_ERROR_CHARS]}"
            raise SlackCallError(msg)
        return body

    def _post(self, payload: dict[str, Any]) -> str:
        body = self._call("chat.postMessage", json={"channel": self._channel, **payload})
        ts = body.get("ts")
        if not isinstance(ts, str):
            msg = "Slack chat.postMessage didn't return the message ts"
            raise SlackCallError(msg)
        return ts

    def _permalink(self, ts: str) -> str:
        body = self._call("chat.getPermalink", params={"channel": self._channel, "message_ts": ts})
        link = body.get("permalink")
        if not isinstance(link, str) or not link.startswith("https://"):
            msg = "Slack chat.getPermalink didn't return a link"
            raise SlackCallError(msg)
        return link

    def post(self, plan: NotificationPlan, tickets: Sequence[TicketOutcome]) -> SlackOutcome:
        """Post the summary, reply with the thread, and fetch the summary's link.

        If the summary fails, nothing else is tried. If it posts but a later step fails, the
        outcome still counts as sent and carries the error.
        """
        try:
            ts = self._post(slack_summary(plan, tickets))
        except SlackCallError as exc:
            return SlackOutcome(error=str(exc))
        errors = []
        if plan.thread:
            try:
                self._post({**slack_thread(plan), "thread_ts": ts})
            except SlackCallError as exc:
                errors.append(str(exc))
        permalink = None
        try:
            permalink = self._permalink(ts)
        except SlackCallError as exc:
            errors.append(str(exc))
        return SlackOutcome(sent=True, permalink=permalink, error="; ".join(errors) or None)
