"""Jira Cloud REST API v3 client. Implements the agent's :class:`TicketTracker`.

Calls, all under ``/rest/api/3``:

- ``GET search/jql``: the newest ticket with an issue's ``sig-<hash>`` label. The older
  ``/search`` endpoint has been removed from Jira Cloud.
- ``POST issue``: a new ticket, with an ADF description from ``payloads.py``.
- ``POST issue/{key}/comment``: "seen again" on an open ticket.
- ``POST issueLink``: a regression "relates to" the closed ticket it repeats.
- ``POST issue/{key}/remotelink``: links back to the Slack thread and the trace. The URL is
  the link's global ID, so adding the same link twice updates it instead of duplicating it.
- ``PUT issue/{key}/properties/incident_suite.slack``: the Slack channel and ``ts`` that first
  reported a new ticket, so the assistant can find that thread on a later run.

The assistant (plan section 17.2) also reads, never writes: the stored thread, and a ticket's
status, assignee and latest comments, only for tickets labelled ``incident-suite``.

Every failure becomes a :class:`TrackerError` with a message that's safe to show. The token
never appears in one.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from incident_suite.agents.assistant import ReaderError, TicketUpdate
from incident_suite.agents.jira_agent import ExistingTicket, TicketDraft, TrackerError
from incident_suite.config import AppConfig
from incident_suite.integrations import TIMEOUT, Sleep, send_with_retry
from incident_suite.integrations.payloads import (
    jira_comment,
    jira_description,
    jira_priority,
    jira_summary,
)

LABEL = "incident-suite"
REGRESSION_LABEL = "regression"
LINK_TYPE = "Relates"
SLACK_PROPERTY = "incident_suite.slack"
MAX_COMMENTS = 3
_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]+$")
_HASH = re.compile(r"^[0-9a-f]{8,16}$")
_CHANNEL = re.compile(r"^[A-Z0-9]{2,20}$")
_TS = re.compile(r"^\d+\.\d+$")
_MAX_ERROR_CHARS = 200


def _checked_key(key: str) -> str:
    """Keys go into URL paths, so only well-formed ones are allowed."""
    if not _KEY.fullmatch(key):
        msg = f"not a JIRA key: {key[:20]!r}"
        raise TrackerError(msg)
    return key


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    messages = [str(m) for m in body.get("errorMessages", [])]
    errors = body.get("errors")
    if isinstance(errors, dict):
        messages += [f"{field}: {text}" for field, text in errors.items()]
    return "; ".join(messages)[:_MAX_ERROR_CHARS]


class JiraClient:
    """Talks to one Jira Cloud project as the bot account."""

    def __init__(
        self,
        config: AppConfig,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        base_url, email = config.jira_base_url, config.jira_email
        token, project = config.jira_api_token, config.jira_project_key
        if base_url is None or email is None or token is None or project is None:
            msg = "JIRA credentials are incomplete"
            raise ValueError(msg)
        if not _PROJECT.fullmatch(project):
            msg = "JIRA_PROJECT_KEY must be capital letters and digits, such as OPS"
            raise ValueError(msg)
        self._base_url = base_url
        self._project = project
        self._issue_type = config.jira_issue_type
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=f"{base_url}/rest/api/3/",
            auth=httpx.BasicAuth(email, token.get_secret_value()),
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
            transport=transport,
        )

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def _call(
        self, method: str, path: str, missing_ok: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        """One request. With ``missing_ok``, a 404 returns ``{}`` instead of raising."""
        try:
            response = send_with_retry(self._http, method, path, self._sleep, **kwargs)
        except httpx.HTTPError as exc:
            msg = f"JIRA couldn't be reached ({type(exc).__name__})"
            raise TrackerError(msg) from exc
        if missing_ok and response.status_code == httpx.codes.NOT_FOUND:
            return {}
        if response.is_error:
            detail = _error_detail(response)
            msg = f"JIRA returned {response.status_code}" + (f": {detail}" if detail else "")
            raise TrackerError(msg)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            msg = "JIRA sent an answer that isn't JSON"
            raise TrackerError(msg) from exc
        return body if isinstance(body, dict) else {}

    def _browse_url(self, key: str) -> str:
        return f"{self._base_url}/browse/{key}"

    def find_latest(self, signature_hash: str) -> ExistingTicket | None:
        """The newest ticket labelled ``sig-<signature_hash>`` in the project, or ``None``."""
        if not _HASH.fullmatch(signature_hash):
            msg = "not a signature hash"
            raise TrackerError(msg)
        jql = (
            f'project = "{self._project}" AND labels = "sig-{signature_hash}" ORDER BY created DESC'
        )
        body = self._call(
            "GET", "search/jql", params={"jql": jql, "fields": "status,comment", "maxResults": 1}
        )
        issues = body.get("issues") or []
        if not issues:
            return None
        try:
            key = _checked_key(str(issues[0]["key"]))
            category = issues[0]["fields"]["status"]["statusCategory"]["key"]
        except (KeyError, TypeError) as exc:
            msg = "JIRA search answer is missing the key or status"
            raise TrackerError(msg) from exc
        done = category == "done"
        comments = ((issues[0]["fields"].get("comment") or {}).get("comments")) or []
        last = adf_text(comments[-1].get("body")) if done and comments else ""
        return ExistingTicket(
            key=key, url=self._browse_url(key), done=done, last_comment=last or None
        )

    def create(self, draft: TicketDraft, regression_of: str | None) -> tuple[str, str]:
        """Open a ticket and return its key and browse URL."""
        labels = [LABEL]
        if draft.issue.signature_hash:
            labels.append(f"sig-{draft.issue.signature_hash}")
        if regression_of is not None:
            labels.append(REGRESSION_LABEL)
        fields = {
            "project": {"key": self._project},
            "issuetype": {"name": self._issue_type},
            "summary": jira_summary(draft),
            "priority": {"name": jira_priority(draft.issue.severity)},
            "labels": labels,
            "description": jira_description(draft, regression_of),
        }
        body = self._call("POST", "issue", json={"fields": fields})
        key = _checked_key(str(body.get("key", "")))
        return key, self._browse_url(key)

    def comment(self, key: str, draft: TicketDraft) -> None:
        """Add "seen again" to an open ticket."""
        self._call("POST", f"issue/{_checked_key(key)}/comment", json={"body": jira_comment(draft)})

    def link_regression(self, key: str, closed_key: str) -> None:
        """Link a regression to the closed ticket it repeats."""
        link = {
            "type": {"name": LINK_TYPE},
            "inwardIssue": {"key": _checked_key(key)},
            "outwardIssue": {"key": _checked_key(closed_key)},
        }
        self._call("POST", "issueLink", json=link)

    def add_remote_link(self, key: str, url: str, title: str) -> None:
        """Link a ticket to a page outside JIRA. Only https links we built are accepted."""
        if not url.startswith("https://"):
            msg = "remote links must be https"
            raise TrackerError(msg)
        link = {"globalId": url, "object": {"url": url, "title": title}}
        self._call("POST", f"issue/{_checked_key(key)}/remotelink", json=link)

    # ------------------------------------------------------------------ Slack thread property

    def set_slack_thread(self, key: str, channel: str, ts: str) -> None:
        """Remember on the ticket which Slack message first reported it."""
        value = {"channel": channel, "ts": ts}
        self._call("PUT", f"issue/{_checked_key(key)}/properties/{SLACK_PROPERTY}", json=value)

    # ------------------------------------------------------------------ for the assistant

    def slack_thread(self, key: str) -> tuple[str, str] | None:
        """The Slack ``(channel, ts)`` stored on a ticket, or ``None`` if there isn't one."""
        try:
            path = f"issue/{_checked_key(key)}/properties/{SLACK_PROPERTY}"
            value = self._call("GET", path, missing_ok=True).get("value")
        except TrackerError as exc:
            raise ReaderError(str(exc)) from exc
        if not isinstance(value, dict):
            return None
        channel, ts = str(value.get("channel", "")), str(value.get("ts", ""))
        if not _CHANNEL.fullmatch(channel) or not _TS.fullmatch(ts):
            return None
        return channel, ts

    def updates(self, key: str) -> TicketUpdate:
        """Status, assignee and the latest comments of one of our own tickets."""
        try:
            body = self._call(
                "GET",
                f"issue/{_checked_key(key)}",
                params={"fields": "status,assignee,labels,comment"},
            )
        except TrackerError as exc:
            raise ReaderError(str(exc)) from exc
        fields = body.get("fields") or {}
        if LABEL not in (fields.get("labels") or []):
            msg = f"{key} isn't a TriageGraph ticket"
            raise ReaderError(msg)
        comments = (fields.get("comment") or {}).get("comments") or []
        return TicketUpdate(
            key=key,
            status=str((fields.get("status") or {}).get("name", "unknown")),
            assignee=str((fields.get("assignee") or {}).get("displayName", "unassigned")),
            comments=tuple(adf_text(c.get("body")) for c in comments[-MAX_COMMENTS:]),
        )


def adf_text(node: Any) -> str:
    """The plain text inside a JIRA ADF document, one line per paragraph."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        parts = [adf_text(child) for child in node.get("content", [])]
        joiner = "\n" if node.get("type") in {"doc", "bulletList", "orderedList"} else " "
        return joiner.join(part for part in parts if part)
    return ""
