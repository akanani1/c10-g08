"""Tests for incident_suite/integrations/payloads.py: safe text, Slack blocks and JIRA documents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from incident_suite.agents.jira_agent import TicketDraft
from incident_suite.integrations.payloads import (
    JIRA_SUMMARY_CHARS,
    MORE_IN_APP,
    SLACK_HEADER_CHARS,
    SLACK_MAX_SECTIONS,
    SLACK_SECTION_CHARS,
    defang,
    jira_comment,
    jira_description,
    jira_priority,
    jira_summary,
    slack_escape,
    slack_summary,
    slack_thread,
)
from incident_suite.state import (
    Category,
    Issue,
    LogLevel,
    NotificationPlan,
    Pattern,
    Remediation,
    RunbookSource,
    Severity,
    TicketAction,
    TicketOutcome,
)

ISSUE = Issue(
    id="ISS-1",
    title="DB pool exhausted, see http://evil.example/x",
    category=Category.DATABASE,
    severity=Severity.CRITICAL,
    confidence=0.9,
    evidence_lines=[12, 13],
    affected_service="payment-api",
    signature_hash="a1b2c3d4e5f6",
)
FIX = Remediation(
    issue_id="ISS-1",
    root_cause="Pool too small after the deploy (L12).",
    steps=["Roll back to v2.14.0", "Raise the pool size"],
    rationale="The errors start right after the deploy.",
    sources=[RunbookSource(runbook="db_connection_pool", score=0.82)],
    confidence=0.8,
)
PATTERN = Pattern(
    signature="Connection refused to db:<port>",
    signature_hash="a1b2c3d4e5f6",
    level=LogLevel.ERROR,
    count=412,
    first_seen=datetime(2026, 10, 9, 14, 0, tzinfo=UTC),
    last_seen=datetime(2026, 10, 9, 14, 7, 30, tzinfo=UTC),
    example_line_nos=[12],
)
PLAN = NotificationPlan(
    post=True,
    reason="At least one medium or worse issue.",
    summary="[CRITICAL] 1 issue(s) in app.log (run run-1)\n- ISS-1 <!channel> see https://x.example",
    thread="Do now:\n- Roll back (ISS-1)",
)


def _texts(node: Any) -> list[str]:
    """Every string value anywhere in a payload."""
    if isinstance(node, dict):
        return [t for value in node.values() for t in _texts(value)]
    if isinstance(node, list):
        return [t for value in node for t in _texts(value)]
    return [node] if isinstance(node, str) else []


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("see http://evil.example/a", "see http[:]//evil.example/a"),
        ("HTTPS://x.example", "HTTPS[:]//x.example"),
        ("go to www.evil.example", "go to www[.]evil.example"),
        ("ftp://files.example", "ftp[:]//files.example"),
        ("no link here: a:b", "no link here: a:b"),
    ],
)
def test_defang(raw: str, safe: str) -> None:
    assert defang(raw) == safe


def test_slack_escape_blocks_mentions_and_fake_links() -> None:
    assert slack_escape("<!channel> <http://x.example|click> & co") == (
        "&lt;!channel&gt; &lt;http[:]//x.example|click&gt; &amp; co"
    )


def test_slack_summary_keeps_log_text_plain() -> None:
    tickets = [
        TicketOutcome(
            issue_id="ISS-1",
            action=TicketAction.CREATE,
            key="OPS-101",
            url="https://team.atlassian.net/browse/OPS-101",
            sent=True,
        )
    ]
    payload = slack_summary(PLAN, tickets)
    blocks = payload["blocks"]

    assert payload["unfurl_links"] is False
    assert payload["text"] == "[CRITICAL] 1 issue(s) in app.log (run run-1)"
    assert blocks[0]["type"] == "header"
    assert blocks[1]["text"] == {
        "type": "plain_text",
        "text": "- ISS-1 <!channel> see https[:]//x.example",
        "emoji": False,
    }
    assert blocks[2]["text"]["type"] == "mrkdwn"
    assert (
        "<https://team.atlassian.net/browse/OPS-101|OPS-101> new ticket"
        in (blocks[2]["text"]["text"])
    )
    assert "://x.example" not in json.dumps(payload)


def test_slack_summary_ticket_lines() -> None:
    tickets = [
        TicketOutcome(
            issue_id="ISS-1",
            action=TicketAction.REGRESSION,
            key="OPS-102",
            url="https://team.atlassian.net/browse/OPS-102",
            related_key="OPS-7",
            sent=True,
        ),
        TicketOutcome(issue_id="ISS-2", action=TicketAction.CREATE, error="JIRA returned <503>"),
        TicketOutcome(issue_id="ISS-3", action=TicketAction.SKIPPED_OVER_CAP),
        TicketOutcome(issue_id="ISS-4", action=TicketAction.CREATE),
    ]
    text = slack_summary(PLAN, tickets)["blocks"][2]["text"]["text"]
    assert text.splitlines() == [
        "*JIRA*",
        "ISS-1: <https://team.atlassian.net/browse/OPS-102|OPS-102> regression of OPS-7",
        "ISS-2: ticket failed (JIRA returned &lt;503&gt;)",
    ]


def test_slack_summary_without_tickets_has_no_jira_block() -> None:
    blocks = slack_summary(PLAN, [])["blocks"]
    assert [b["type"] for b in blocks] == ["header", "section", "context"]


def test_slack_limits() -> None:
    long_title = "x" * 400
    huge_line = "y" * (SLACK_SECTION_CHARS + 500)
    many_lines = "\n".join(f"line {n} " + "z" * 2_000 for n in range(120))
    plan = PLAN.model_copy(update={"summary": f"{long_title}\n{huge_line}", "thread": many_lines})

    summary = slack_summary(plan, [])["blocks"]
    assert len(summary[0]["text"]["text"]) == SLACK_HEADER_CHARS
    assert summary[1]["text"]["text"].endswith(MORE_IN_APP)
    assert len(summary[1]["text"]["text"]) == SLACK_SECTION_CHARS

    thread = slack_thread(plan)["blocks"]
    assert len(thread) == SLACK_MAX_SECTIONS
    assert all(len(b["text"]["text"]) <= SLACK_SECTION_CHARS for b in thread)
    assert thread[-1]["text"]["text"] == MORE_IN_APP


def test_jira_summary_and_priority() -> None:
    draft = TicketDraft(issue=ISSUE, remediation=FIX, pattern=PATTERN, run_id="run-1")
    assert jira_summary(draft) == (
        "[CRITICAL] DB pool exhausted, see http[:]//evil.example/x (payment-api)"
    )
    messy = ISSUE.model_copy(update={"title": "a\nb  " + "c" * 300, "affected_service": None})
    summary = jira_summary(TicketDraft(issue=messy, remediation=None, pattern=None, run_id="r"))
    assert "\n" not in summary
    assert len(summary) == JIRA_SUMMARY_CHARS
    assert [jira_priority(s) for s in Severity] == ["Low", "Medium", "High", "Highest"]


def test_jira_description_has_every_part() -> None:
    draft = TicketDraft(issue=ISSUE, remediation=FIX, pattern=PATTERN, run_id="run-1")
    doc = jira_description(draft, regression_of="OPS-7")
    texts = _texts(doc)

    assert doc["type"] == "doc"
    assert doc["version"] == 1
    for expected in (
        "What happened",
        "Seen 412 times between 2026-10-09 14:00:00 and 14:07:30.",
        "Pattern: Connection refused to db:<port>",
        "Regression: this was seen before in OPS-7.",
        "Log lines L12, L13",
        "Roll back to v2.14.0",
        "db_connection_pool (match 0.82)",
        "Opened by Incident Suite, run run-1.",
    ):
        assert expected in texts
    assert all(t for t in texts)  # ADF rejects empty text nodes
    assert "://evil" not in json.dumps(doc)
    assert "marks" not in json.dumps(doc)  # no links built from log text


def test_jira_description_without_fix_pattern_or_runbook() -> None:
    no_source = FIX.model_copy(update={"sources": []})
    draft = TicketDraft(issue=ISSUE, remediation=no_source, pattern=None, run_id="run-1")
    texts = _texts(jira_description(draft, regression_of=None))
    assert "No runbook matched." in texts
    assert not any(t.startswith(("Seen", "Regression")) for t in texts)

    bare = TicketDraft(issue=ISSUE, remediation=None, pattern=None, run_id="run-1")
    assert "No fix was suggested for this issue. See the app." in _texts(
        jira_description(bare, None)
    )


def test_jira_evidence_is_capped() -> None:
    many = ISSUE.model_copy(update={"evidence_lines": list(range(1, 41))})
    draft = TicketDraft(issue=many, remediation=None, pattern=None, run_id="run-1")
    assert any(t.endswith("L30 and 10 more") for t in _texts(jira_description(draft, None)))


def test_jira_comment() -> None:
    draft = TicketDraft(issue=ISSUE, remediation=FIX, pattern=PATTERN, run_id="run-2")
    texts = _texts(jira_comment(draft))
    assert "Seen again in run run-2." in texts
    assert "Seen 412 times between 2026-10-09 14:00:00 and 14:07:30." in texts

    no_times = PATTERN.model_copy(update={"first_seen": None, "last_seen": None})
    draft = TicketDraft(issue=ISSUE, remediation=None, pattern=no_times, run_id="run-2")
    assert "Seen 412 times." in _texts(jira_comment(draft))
