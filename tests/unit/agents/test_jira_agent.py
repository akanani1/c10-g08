"""Tests for incident_suite/agents/jira_agent.py: the rules in build plan section 4.2."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from incident_suite.agents.jira_agent import (
    ExistingTicket,
    TicketTracker,
    plan_tickets,
    send_tickets,
)
from incident_suite.state import Category, Issue, RunSettings, Severity, TicketAction

MakeTracker = Callable[..., tuple[TicketTracker, list[tuple[str, str]]]]


def _issue(number: int, severity: Severity = Severity.CRITICAL, **extra: Any) -> Issue:
    fields: dict[str, Any] = {
        "id": f"ISS-{number}",
        "title": f"Problem {number}",
        "category": Category.DATABASE,
        "severity": severity,
        "confidence": 0.9,
        "evidence_lines": [number],
        "signature_hash": f"{number:012x}",
    }
    return Issue.model_validate(fields | extra)


OPEN = ExistingTicket(key="OPS-12", url="https://team.atlassian.example/browse/OPS-12", done=False)
CLOSED = ExistingTicket(key="OPS-7", url="https://team.atlassian.example/browse/OPS-7", done=True)


def test_new_open_and_closed_tickets(settings: RunSettings, make_tracker: MakeTracker) -> None:
    tracker, calls = make_tracker(existing={f"{2:012x}": OPEN, f"{3:012x}": CLOSED})
    plan = plan_tickets([_issue(1), _issue(2), _issue(3)], settings, tracker)

    assert [(p.issue_id, p.action) for p in plan] == [
        ("ISS-1", TicketAction.CREATE),
        ("ISS-2", TicketAction.COMMENT),
        ("ISS-3", TicketAction.REGRESSION),
    ]
    assert plan[1].key == "OPS-12"
    assert plan[2].related_key == "OPS-7"
    assert all(not p.sent for p in plan)
    assert [method for method, _ in calls] == ["find", "find", "find"]


def test_only_critical_by_default_and_worst_first(
    settings: RunSettings, make_tracker: MakeTracker
) -> None:
    tracker, _ = make_tracker()
    plan = plan_tickets([_issue(1, Severity.HIGH), _issue(2)], settings, tracker)
    assert [p.issue_id for p in plan] == ["ISS-2"]

    high_too = settings.model_copy(
        update={"jira": settings.jira.model_copy(update={"min_severity": Severity.HIGH})}
    )
    plan = plan_tickets([_issue(1, Severity.HIGH), _issue(2)], high_too, tracker)
    assert [p.issue_id for p in plan] == ["ISS-2", "ISS-1"]


def test_cap_of_three(settings: RunSettings, make_tracker: MakeTracker) -> None:
    tracker, calls = make_tracker()
    plan = plan_tickets([_issue(n) for n in range(1, 6)], settings, tracker)
    assert [p.action for p in plan].count(TicketAction.SKIPPED_OVER_CAP) == 2
    assert len(calls) == 3


def test_dry_run_without_a_tracker(settings: RunSettings) -> None:
    plan = plan_tickets([_issue(1), _issue(2, signature_hash=None)], settings, None)
    assert {p.action for p in plan} == {TicketAction.CREATE}


def test_jira_down_while_planning(settings: RunSettings, make_tracker: MakeTracker) -> None:
    tracker, _ = make_tracker(fail="find")
    (item,) = plan_tickets([_issue(1)], settings, tracker)
    assert item.error == "JIRA returned 503"


def test_send_carries_out_the_plan(settings: RunSettings, make_tracker: MakeTracker) -> None:
    issues = [_issue(n) for n in range(1, 6)]
    tracker, calls = make_tracker(existing={f"{2:012x}": OPEN, f"{3:012x}": CLOSED})
    plan = plan_tickets(issues, settings, tracker)
    calls.clear()

    done = send_tickets(plan, issues, [], "run-1", tracker)

    assert calls == [("create", "ISS-1<--"), ("comment", "OPS-12"), ("create", "ISS-3<-OPS-7")]
    assert [d.sent for d in done] == [True, True, True, False, False]
    assert done[0].key == "OPS-101"
    assert done[0].url == "https://team.atlassian.example/browse/OPS-101"


def test_failed_items_are_not_retried(settings: RunSettings, make_tracker: MakeTracker) -> None:
    searching, _ = make_tracker(fail="find")
    plan = plan_tickets([_issue(1)], settings, searching)
    writer, calls = make_tracker()
    (item,) = send_tickets(plan, [_issue(1)], [], "run-1", writer)
    assert calls == []
    assert not item.sent


def test_write_failure_is_recorded(settings: RunSettings, make_tracker: MakeTracker) -> None:
    tracker, _ = make_tracker(fail="create")
    plan = plan_tickets([_issue(1)], settings, tracker)
    (item,) = send_tickets(plan, [_issue(1)], [], "run-1", tracker)
    assert item.error == "JIRA returned 503"
    assert not item.sent
