"""Tests for incident_suite/ui.py: escaping, confidence reasons, the pipeline and the tables."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from incident_suite import ui
from incident_suite.state import (
    AgentUsage,
    Category,
    Issue,
    LogLevel,
    LogLine,
    Pattern,
    Remediation,
    RunbookSource,
    RunSettings,
    RunStep,
    Severity,
    StepStatus,
    TicketAction,
    TicketOutcome,
)

ISSUE = Issue(
    id="ISS-1",
    title="DB pool exhausted",
    category=Category.DATABASE,
    severity=Severity.CRITICAL,
    confidence=0.7,
    evidence_lines=[12, 13],
    affected_service="payment-api",
    signature_hash="a1b2c3d4e5f6",
)
FIX = Remediation(
    issue_id="ISS-1",
    root_cause="Pool too small.",
    steps=["Roll back"],
    rationale="Errors start after the deploy.",
    sources=[
        RunbookSource(runbook="db_connection_pool", score=0.82),
        RunbookSource(runbook="deploy_rollback", score=0.41),
    ],
    confidence=0.85,
)


def _step(node: str, status: StepStatus = StepStatus.DONE, seconds: float = 1.0) -> RunStep:
    return RunStep(node=node, status=status, seconds=seconds, detail=f"{node} done")


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("[click](http://evil.example)", "\\[click\\]\\(http\\[\\:\\]//evil\\.example\\)"),
        ("<script>alert(1)</script>", "\\<script\\>alert\\(1\\)\\</script\\>"),
        ("**bold** _it_ `code` # head", "\\*\\*bold\\*\\* \\_it\\_ \\`code\\` \\# head"),
        (":red[x] $x$", "\\:red\\[x\\] \\$x\\$"),
        ("two\nlines", "two  \nlines"),
    ],
)
def test_md_escapes_everything(raw: str, safe: str) -> None:
    assert ui.md(raw) == safe


def test_badges_money_and_labels() -> None:
    assert ui.severity_badge(Severity.CRITICAL) == ":red-badge[CRITICAL]"
    assert ui.severity_badge(Severity.LOW) == ":gray-badge[LOW]"
    assert ui.money(0.01234) == "$0.0123"
    assert ui.money(2.5) == "$2.50"
    assert ui.evidence_label([3, 4], structured=False) == "lines 3, 4"
    assert ui.evidence_label([3], structured=True) == "rows 3"


def test_confidence_and_reasons() -> None:
    assert ui.confidence(ISSUE, FIX) == 0.85
    assert ui.confidence(ISSUE, None) == 0.7
    assert ui.confidence_reasons(ISSUE, FIX, None, dry_run=True) == [
        "2 evidence lines support it",
        "Best runbook match 0.82 (db_connection_pool)",
    ]
    one_line = ISSUE.model_copy(update={"evidence_lines": [12]})
    no_runbook = FIX.model_copy(update={"sources": []})
    assert ui.confidence_reasons(one_line, no_runbook, None, dry_run=True) == [
        "1 evidence line support it",
        "No runbook matched",
    ]
    assert ui.confidence_reasons(ISSUE, None, None, dry_run=True)[1] == "No fix was suggested"


@pytest.mark.parametrize(
    ("ticket", "dry_run", "reason"),
    [
        (
            TicketOutcome(issue_id="ISS-1", action=TicketAction.COMMENT, key="SCRUM-4"),
            False,
            "Seen before: SCRUM-4 is still open",
        ),
        (
            TicketOutcome(issue_id="ISS-1", action=TicketAction.REGRESSION, related_key="SCRUM-2"),
            False,
            "Seen before: SCRUM-2 was closed, so this is a regression",
        ),
        (TicketOutcome(issue_id="ISS-1", action=TicketAction.CREATE), False, "Not seen before"),
        (TicketOutcome(issue_id="ISS-1", action=TicketAction.CREATE), True, None),
    ],
)
def test_jira_history_reason(ticket: TicketOutcome, dry_run: bool, reason: str | None) -> None:
    reasons = ui.confidence_reasons(ISSUE, FIX, ticket, dry_run)
    if reason is None:
        assert len(reasons) == 2  # a dry run never asked JIRA, so it claims nothing
    else:
        assert reasons[-1].startswith(reason)


def test_ticket_preview() -> None:
    planned = TicketOutcome(issue_id="ISS-1", action=TicketAction.CREATE)
    assert ui.ticket_preview(planned, ISSUE, "run-1") == (
        "ISS-1 -> new ticket. Title: [CRITICAL] DB pool exhausted (payment-api)"
    )
    commented = TicketOutcome(
        issue_id="ISS-1", action=TicketAction.COMMENT, key="SCRUM-4", sent=True
    )
    assert ui.ticket_preview(commented, ISSUE, "run-1").startswith(
        "ISS-1 -> SCRUM-4: comment on the open ticket (sent)."
    )
    failed = planned.model_copy(update={"error": "JIRA returned 503"})
    assert "(failed: JIRA returned 503)" in ui.ticket_preview(failed, ISSUE, "run-1")


def test_working_nodes_follow_the_graph() -> None:
    assert ui.working_nodes([], running=True) == {"log_reader"}
    after_fix = [_step("log_reader"), _step("remediation")]
    assert ui.working_nodes(after_fix, running=True) == {"cookbook", "jira_plan"}
    assert ui.working_nodes(after_fix, running=False) == set()


def test_pipeline_dot_colours_each_state() -> None:
    steps = [
        _step("log_reader"),
        _step("remediation"),
        _step("jira_plan", StepStatus.SKIPPED),
        _step("cookbook", StepStatus.FAILED),
    ]
    dot = ui.pipeline_dot(steps, running=True)
    assert dot.startswith("digraph pipeline {")
    assert 'log_reader [label="Log Reader", fillcolor="#166534"]' in dot
    assert (
        'jira_plan [label="JIRA plan", fillcolor="#3f3f46", style="rounded,filled,dashed"]' in dot
    )
    assert 'cookbook [label="Cookbook", fillcolor="#991b1b"]' in dot
    assert 'notification [label="Notification plan", fillcolor="#c2410c"]' in dot  # working
    assert 'approval [label="Approval", fillcolor="#27272a"]' in dot  # waiting
    assert 'remediation -> remediation [label="more context"]' in dot


def test_pipeline_strip_and_run_log() -> None:
    steps = [
        _step("log_reader", seconds=1.0),
        _step("remediation"),
        _step("log_reader", seconds=0.5),
    ]
    strip = ui.pipeline_strip(steps)
    assert strip[0] == {"Step": "Log Reader", "Status": "done", "Seconds": 1.5}
    assert strip[-1] == {"Step": "Send", "Status": "not run", "Seconds": 0.0}
    log = ui.run_log_rows(steps)
    assert [row["#"] for row in log] == [1, 2, 3]
    assert log[1]["Agent"] == "Remediation"


def test_tables() -> None:
    usage = [
        AgentUsage(agent="log_reader", model="m", calls=1, input_tokens=100, cost_usd=0.001),
        AgentUsage(agent="remediation", calls=2, output_tokens=50, cost_usd=0.002),
    ]
    rows = ui.usage_rows(usage)
    assert rows[1]["Model"] == "-"
    assert rows[-1] == {
        "Agent": "Total",
        "Model": "",
        "Calls": 3,
        "Tokens in": 100,
        "Tokens out": 50,
        "Cost": "$0.0030",
        "Seconds": 0.0,
    }

    when = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
    patterns = [
        Pattern(
            signature=s,
            signature_hash=f"{n:012x}",
            level=LogLevel.ERROR,
            count=n,
            first_seen=when if n == 9 else None,
            last_seen=when if n == 9 else None,
            example_line_nos=[1],
        )
        for n, s in ((2, "small"), (9, "big"))
    ]
    assert [row["Pattern"] for row in ui.pattern_rows(patterns)] == ["big", "small"]
    assert ui.pattern_rows(patterns)[1]["First seen"] == ""

    lines = [
        LogLine(line_no=n, raw="x", message="x", host="h1", timestamp=when if n == 1 else None)
        for n in (1, 2, 3)
    ]
    assert len(ui.line_rows(lines, limit=2)) == 2
    assert ui.line_rows(lines)[0]["Service"] == "h1"
    assert ui.line_rows(lines)[1]["Time"] == ""

    second = FIX.model_copy(
        update={
            "issue_id": "ISS-2",
            "sources": [RunbookSource(runbook="deploy_rollback", score=0.6)],
        }
    )
    assert ui.source_rows([FIX, second]) == [
        {"Runbook": "db_connection_pool", "Best match": 0.82, "Used for": "ISS-1"},
        {"Runbook": "deploy_rollback", "Best match": 0.6, "Used for": "ISS-1, ISS-2"},
    ]


# ------------------------------------------------------------------ settings page


def test_model_label_and_prompt_override() -> None:
    assert ui.model_label("openai/gpt-5-mini") == (
        "openai/gpt-5-mini ($0.25 in / $2.00 out per M tokens)"
    )
    assert ui.prompt_override("  Default prompt\n", "Default prompt") is None
    assert ui.prompt_override("Be brief.", "Default prompt") == "Be brief."


def test_settings_json_round_trip(settings: RunSettings) -> None:
    edited = settings.model_copy(update={"token_budget": 50_000})
    assert ui.settings_from_json(ui.settings_to_json(edited).encode()) == edited


@pytest.mark.parametrize(
    ("change", "where"),
    [
        ({"remediation": {"model": "evil/free-model", "top_k": 3}}, "remediation.model"),
        ({"token_budget": 10_000_000}, "token_budget"),
        ({"jira": {"max_tickets": 50}}, "jira.max_tickets"),
    ],
)
def test_settings_import_is_checked(
    settings: RunSettings, change: dict[str, Any], where: str
) -> None:
    raw = json.loads(ui.settings_to_json(settings))
    for key, value in change.items():
        raw[key] = raw[key] | value if isinstance(value, dict) else value
    with pytest.raises(ValueError, match=f"isn't valid \\({re.escape(where)}"):
        ui.settings_from_json(json.dumps(raw).encode())
    with pytest.raises(ValueError, match="isn't valid"):
        ui.settings_from_json(b"not json")


def test_decide_live(settings: RunSettings) -> None:
    live = settings.model_copy(update={"live": True})
    edited = live.model_copy(
        update={"cookbook": live.cookbook.model_copy(update={"system_prompt": "Be brief."})}
    )
    assert ui.decide_live(settings, live_allowed=True, admin_ok=True) == (False, "")
    assert ui.decide_live(live, live_allowed=False, admin_ok=True) == (False, "")
    assert ui.decide_live(live, live_allowed=True, admin_ok=False) == (True, "")
    allowed, note = ui.decide_live(edited, live_allowed=True, admin_ok=False)
    assert not allowed
    assert note.startswith("Prompts were edited")
    assert ui.decide_live(edited, live_allowed=True, admin_ok=True) == (True, "")
