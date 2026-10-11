"""Tests for incident_suite/agents/notification.py: when to post, and what."""

from __future__ import annotations

from collections.abc import Callable

from incident_suite.agents.notification import (
    Messenger,
    plan_notification,
    send_notification,
)
from incident_suite.state import (
    Category,
    Checklist,
    ChecklistItem,
    Issue,
    NotificationPlan,
    Remediation,
    RunbookSource,
    Severity,
    TicketAction,
    TicketOutcome,
)

MakeMessenger = Callable[..., tuple[Messenger, list[NotificationPlan]]]


def _issue(number: int, severity: Severity) -> Issue:
    return Issue(
        id=f"ISS-{number}",
        title=f"Problem {number}",
        category=Category.DATABASE,
        severity=severity,
        confidence=0.9,
        evidence_lines=list(range(10, 18)),
        affected_service="payment-api",
    )


FIX = Remediation(
    issue_id="ISS-1",
    root_cause="Likely the pool shrank (L5).",
    steps=["Roll back payment-api", "Raise db.pool.max_size"],
    rationale="Errors start after the deploy.",
    sources=[RunbookSource(runbook="db-pool-exhausted", score=0.7)],
    confidence=0.8,
)
CHECKLIST = Checklist(
    do_now=[ChecklistItem(text="Roll back payment-api", issue_ids=["ISS-1"])],
    prevent=[ChecklistItem(text="Alert on pool use", issue_ids=["ISS-1"])],
)


def test_nothing_to_post() -> None:
    assert not plan_notification([], [], Checklist(), [], "a.log", "run-1").post
    low = plan_notification([_issue(1, Severity.LOW)], [], Checklist(), [], "a.log", "run-1")
    assert not low.post
    assert low.reason.startswith("Only low-severity")


def test_summary_and_thread() -> None:
    tickets = [TicketOutcome(issue_id="ISS-1", action=TicketAction.COMMENT, key="OPS-12")]
    plan = plan_notification(
        [_issue(1, Severity.CRITICAL), _issue(2, Severity.MEDIUM)],
        [FIX],
        CHECKLIST,
        tickets,
        "payment.log",
        "run-1",
    )
    assert plan.post
    first, issue_one, issue_two = plan.summary.splitlines()
    assert first == "[CRITICAL] 2 issue(s) in payment.log (run run-1)"
    assert "evidence L10, L11, L12, L13, L14 (+3 more)" in issue_one
    assert "Fix: Roll back payment-api | OPS-12: updated, seen again" in issue_one
    assert "Fix:" not in issue_two
    assert "Do now:\n- Roll back payment-api (ISS-1)" in plan.thread
    assert "Prevent:\n- Alert on pool use (ISS-1)" in plan.thread
    assert "This week" not in plan.thread
    assert "1. Roll back payment-api\n2. Raise db.pool.max_size" in plan.thread
    assert "Sources: db-pool-exhausted" in plan.thread


def test_more_than_five_issues_are_summarised() -> None:
    issues = [_issue(n, Severity.HIGH) for n in range(1, 8)]
    plan = plan_notification(issues, [], Checklist(), [], "a.log", "run-1")
    assert plan.summary.splitlines()[-1] == "+2 more in the app"


def test_ticket_notes_for_each_action() -> None:
    issues = [_issue(n, Severity.CRITICAL) for n in (1, 2, 3)]
    tickets = [
        TicketOutcome(issue_id="ISS-1", action=TicketAction.CREATE),
        TicketOutcome(issue_id="ISS-2", action=TicketAction.REGRESSION, related_key="OPS-7"),
        TicketOutcome(issue_id="ISS-3", action=TicketAction.SKIPPED_OVER_CAP),
    ]
    summary = plan_notification(issues, [], Checklist(), tickets, "a.log", "r").summary
    assert "JIRA: new ticket" in summary
    assert "OPS-7: regression" in summary
    assert "not ticketed (over the ticket cap)" in summary


def test_send(make_messenger: MakeMessenger) -> None:
    messenger, posts = make_messenger()
    skip = NotificationPlan(post=False, reason="nothing")
    assert not send_notification(skip, [], messenger).sent
    assert posts == []

    plan = NotificationPlan(post=True, reason="medium", summary="s", thread="t")
    outcome = send_notification(plan, [], messenger)
    assert outcome.sent
    assert posts == [plan]
