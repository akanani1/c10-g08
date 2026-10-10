"""Tests for incident_suite/integrations/jira_client.py, against httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from incident_suite.agents.jira_agent import ExistingTicket, TicketDraft, TrackerError
from incident_suite.config import AppConfig
from incident_suite.integrations.jira_client import JiraClient
from incident_suite.state import Category, Issue, Severity

SITE = "https://team.atlassian.net"
ISSUE = Issue(
    id="ISS-1",
    title="DB pool exhausted",
    category=Category.DATABASE,
    severity=Severity.CRITICAL,
    confidence=0.9,
    evidence_lines=[12],
    affected_service="payment-api",
    signature_hash="a1b2c3d4e5f6",
)
DRAFT = TicketDraft(issue=ISSUE, remediation=None, pattern=None, run_id="run-1")

Reply = httpx.Response | Exception
MakeClient = Callable[..., tuple[JiraClient, list[httpx.Request], list[float]]]


@pytest.fixture
def jira_config() -> AppConfig:
    """Complete, fake JIRA credentials."""
    return AppConfig(
        jira_base_url=SITE,
        jira_email="bot@example.com",
        jira_api_token=SecretStr("jira-test-token"),
        jira_project_key="OPS",
    )


@pytest.fixture
def make_client(jira_config: AppConfig) -> MakeClient:
    """``client, requests, waits = make_client(reply, ...)``: replies are used in order."""

    def make(*replies: Reply) -> tuple[JiraClient, list[httpx.Request], list[float]]:
        queue = list(replies)
        requests: list[httpx.Request] = []
        waits: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client = JiraClient(jira_config, transport=httpx.MockTransport(handler), sleep=waits.append)
        return client, requests, waits

    return make


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _search(status_category: str) -> httpx.Response:
    issue = {"key": "OPS-12", "fields": {"status": {"statusCategory": {"key": status_category}}}}
    return httpx.Response(200, json={"issues": [issue], "isLast": True})


def test_needs_complete_credentials(jira_config: AppConfig) -> None:
    with pytest.raises(ValueError, match="incomplete"):
        JiraClient(AppConfig())
    bad_project = jira_config.model_copy(update={"jira_project_key": "ops; drop"})
    with pytest.raises(ValueError, match="JIRA_PROJECT_KEY"):
        JiraClient(bad_project)


def test_find_latest_uses_the_jql_search_endpoint(make_client: MakeClient) -> None:
    client, requests, _ = make_client(_search("indeterminate"))
    found = client.find_latest("a1b2c3d4e5f6")

    assert found == ExistingTicket(key="OPS-12", url=f"{SITE}/browse/OPS-12", done=False)
    (request,) = requests
    assert request.method == "GET"
    assert request.url.path == "/rest/api/3/search/jql"
    assert request.url.params["jql"] == (
        'project = "OPS" AND labels = "sig-a1b2c3d4e5f6" ORDER BY created DESC'
    )
    assert request.url.params["maxResults"] == "1"
    assert request.headers["Authorization"].startswith("Basic ")


def test_find_latest_done_and_missing(make_client: MakeClient) -> None:
    client, _, _ = make_client(_search("done"), httpx.Response(200, json={"issues": []}))
    found = client.find_latest("a1b2c3d4e5f6")
    assert found is not None
    assert found.done
    assert client.find_latest("a1b2c3d4e5f6") is None


@pytest.mark.parametrize("bad", ['x" OR project = "HR', "ABC", ""])
def test_find_latest_rejects_anything_but_a_hash(make_client: MakeClient, bad: str) -> None:
    client, requests, _ = make_client()
    with pytest.raises(TrackerError, match="not a signature hash"):
        client.find_latest(bad)
    assert requests == []


def test_find_latest_odd_answer(make_client: MakeClient) -> None:
    client, _, _ = make_client(httpx.Response(200, json={"issues": [{"key": "OPS-1"}]}))
    with pytest.raises(TrackerError, match="missing the key or status"):
        client.find_latest("a1b2c3d4e5f6")


def test_create(make_client: MakeClient) -> None:
    client, requests, _ = make_client(
        httpx.Response(201, json={"id": "10001", "key": "OPS-101", "self": "..."})
    )
    assert client.create(DRAFT, regression_of="OPS-7") == ("OPS-101", f"{SITE}/browse/OPS-101")

    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == "/rest/api/3/issue"
    fields = _body(request)["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["summary"] == "[CRITICAL] DB pool exhausted (payment-api)"
    assert fields["priority"] == {"name": "Highest"}
    assert fields["labels"] == ["incident-suite", "sig-a1b2c3d4e5f6", "regression"]
    assert fields["description"]["type"] == "doc"


def test_create_without_signature(make_client: MakeClient) -> None:
    client, requests, _ = make_client(httpx.Response(201, json={"key": "OPS-102"}))
    no_hash = TicketDraft(
        issue=ISSUE.model_copy(update={"signature_hash": None}),
        remediation=None,
        pattern=None,
        run_id="run-1",
    )
    client.create(no_hash, regression_of=None)
    assert _body(requests[0])["fields"]["labels"] == ["incident-suite"]


def test_create_with_a_bad_key_in_the_answer(make_client: MakeClient) -> None:
    client, _, _ = make_client(httpx.Response(201, json={"key": "../../admin"}))
    with pytest.raises(TrackerError, match="not a JIRA key"):
        client.create(DRAFT, regression_of=None)


def test_comment_link_and_remote_link(make_client: MakeClient) -> None:
    client, requests, _ = make_client(
        httpx.Response(201, json={"id": "1"}),
        httpx.Response(201),
        httpx.Response(201, json={"id": 5, "self": "..."}),
    )
    client.comment("OPS-12", DRAFT)
    client.link_regression("OPS-101", "OPS-7")
    client.add_remote_link("OPS-101", "https://team.slack.com/archives/C1/p1", "Slack: summary")

    comment, link, remote = requests
    assert comment.url.path == "/rest/api/3/issue/OPS-12/comment"
    assert _body(comment)["body"]["type"] == "doc"
    assert link.url.path == "/rest/api/3/issueLink"
    assert _body(link) == {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": "OPS-101"},
        "outwardIssue": {"key": "OPS-7"},
    }
    assert remote.url.path == "/rest/api/3/issue/OPS-101/remotelink"
    assert _body(remote) == {
        "globalId": "https://team.slack.com/archives/C1/p1",
        "object": {"url": "https://team.slack.com/archives/C1/p1", "title": "Slack: summary"},
    }


def test_keys_and_links_are_checked_before_sending(make_client: MakeClient) -> None:
    client, requests, _ = make_client()
    with pytest.raises(TrackerError, match="not a JIRA key"):
        client.comment("OPS-1/../../x", DRAFT)
    with pytest.raises(TrackerError, match="https"):
        client.add_remote_link("OPS-1", "javascript:alert(1)", "x")
    assert requests == []


def test_error_messages_are_safe_and_short(make_client: MakeClient) -> None:
    client, _, _ = make_client(
        httpx.Response(
            400,
            json={"errorMessages": ["Bad request"], "errors": {"priority": "not on screen"}},
        ),
        httpx.Response(401, text="<html>nope</html>"),
        httpx.Response(404, json=["odd"]),
    )
    with pytest.raises(TrackerError) as first:
        client.create(DRAFT, regression_of=None)
    assert str(first.value) == "JIRA returned 400: Bad request; priority: not on screen"
    with pytest.raises(TrackerError) as second:
        client.comment("OPS-12", DRAFT)
    assert str(second.value) == "JIRA returned 401"
    with pytest.raises(TrackerError, match=r"^JIRA returned 404$"):
        client.comment("OPS-12", DRAFT)
    assert "jira-test-token" not in str(first.value) + str(second.value)


def test_non_json_answer(make_client: MakeClient) -> None:
    client, _, _ = make_client(httpx.Response(200, text="<html>"))
    with pytest.raises(TrackerError, match="isn't JSON"):
        client.find_latest("a1b2c3d4e5f6")


def test_retries_rate_limits_then_succeeds(make_client: MakeClient) -> None:
    client, requests, waits = make_client(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(503),
        _search("done"),
    )
    assert client.find_latest("a1b2c3d4e5f6") is not None
    assert len(requests) == 3
    assert waits == [2.0, 2.0]


def test_gives_up_after_three_tries(make_client: MakeClient) -> None:
    client, requests, _ = make_client(httpx.Response(503), httpx.Response(503), httpx.Response(503))
    with pytest.raises(TrackerError, match="JIRA returned 503"):
        client.find_latest("a1b2c3d4e5f6")
    assert len(requests) == 3


def test_unreachable(make_client: MakeClient) -> None:
    down = httpx.ConnectError("no route")
    client, requests, _ = make_client(down, down, down)
    with pytest.raises(TrackerError, match=r"JIRA couldn't be reached \(ConnectError\)"):
        client.find_latest("a1b2c3d4e5f6")
    assert len(requests) == 3


def test_a_read_timeout_is_not_retried(make_client: MakeClient) -> None:
    client, requests, _ = make_client(httpx.ReadTimeout("slow"))
    with pytest.raises(TrackerError, match="ReadTimeout"):
        client.create(DRAFT, regression_of=None)
    assert len(requests) == 1
    client.close()
