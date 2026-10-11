"""Tests for incident_suite/agents/jira_agent.py: when to create, comment, reopen or skip."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from incident_suite.agents.jira_agent import (
    ExistingTicket,
    TicketDraft,
    TicketTracker,
    link_back,
    plan_tickets,
    remember_thread,
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


def _drafts(issues: list[Issue]) -> dict[str, TicketDraft]:
    return {
        i.id: TicketDraft(issue=i, remediation=None, pattern=None, run_id="run-1") for i in issues
    }


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

    done = send_tickets(plan, _drafts(issues), tracker)

    assert calls == [
        ("create", "ISS-1<--"),
        ("comment", "OPS-12"),
        ("create", "ISS-3<-OPS-7"),
        ("link", "OPS-102->OPS-7"),
    ]
    assert [d.sent for d in done] == [True, True, True, False, False]
    assert done[0].key == "OPS-101"
    assert done[0].url == "https://team.atlassian.example/browse/OPS-101"


def test_failed_items_are_not_retried(settings: RunSettings, make_tracker: MakeTracker) -> None:
    searching, _ = make_tracker(fail="find")
    plan = plan_tickets([_issue(1)], settings, searching)
    writer, calls = make_tracker()
    (item,) = send_tickets(plan, _drafts([_issue(1)]), writer)
    assert calls == []
    assert not item.sent


def test_write_failure_is_recorded(settings: RunSettings, make_tracker: MakeTracker) -> None:
    tracker, _ = make_tracker(fail="create")
    plan = plan_tickets([_issue(1)], settings, tracker)
    (item,) = send_tickets(plan, _drafts([_issue(1)]), tracker)
    assert item.error == "JIRA returned 503"
    assert not item.sent


def test_regression_link_failure_keeps_the_ticket(
    settings: RunSettings, make_tracker: MakeTracker
) -> None:
    tracker, _ = make_tracker(existing={f"{1:012x}": CLOSED}, fail="link")
    plan = plan_tickets([_issue(1)], settings, tracker)
    (item,) = send_tickets(plan, _drafts([_issue(1)]), tracker)
    assert item.sent
    assert item.key == "OPS-101"
    assert item.error == "OPS-101 created, but not linked to OPS-7: JIRA returned 503"


def test_link_back_only_touches_sent_tickets(
    settings: RunSettings, make_tracker: MakeTracker
) -> None:
    issues = [_issue(n) for n in range(1, 5)]
    tracker, calls = make_tracker(existing={f"{2:012x}": OPEN})
    done = send_tickets(plan_tickets(issues, settings, tracker), _drafts(issues), tracker)
    calls.clear()

    errors = link_back(done, [("https://slack.example/p1", "Slack")], tracker)

    assert errors == []
    assert calls == [
        ("remote_link", "OPS-101->https://slack.example/p1"),
        ("remote_link", "OPS-12->https://slack.example/p1"),
        ("remote_link", "OPS-102->https://slack.example/p1"),
    ]


def test_link_back_failure_is_reported(settings: RunSettings, make_tracker: MakeTracker) -> None:
    tracker, _ = make_tracker(fail="remote_link")
    done = send_tickets(plan_tickets([_issue(1)], settings, tracker), _drafts([_issue(1)]), tracker)
    errors = link_back(done, [("https://slack.example/p1", "Slack")], tracker)
    assert errors == ["JIRA (OPS-101): link to Slack not added: JIRA returned 503"]


def test_remember_thread_only_on_new_tickets(
    settings: RunSettings, make_tracker: MakeTracker
) -> None:
    issues = [_issue(1), _issue(2)]
    tracker, calls = make_tracker(existing={f"{2:012x}": OPEN})
    done = send_tickets(plan_tickets(issues, settings, tracker), _drafts(issues), tracker)
    calls.clear()
    assert remember_thread(done, "C1", "1.2", tracker) == []
    assert calls == [("slack_thread", "OPS-101->C1/1.2")]  # SCRUM-12 keeps its first thread

    failing, _ = make_tracker(fail="slack_thread")
    assert remember_thread(done, "C1", "1.2", failing) == [
        "JIRA (OPS-101): Slack thread not stored: JIRA returned 503"
    ]


def test_a_regression_carries_the_previous_fix(
    settings: RunSettings, make_tracker: MakeTracker
) -> None:
    fixed = ExistingTicket(
        key="SCRUM-1",
        url="https://team.atlassian.example/browse/SCRUM-1",
        done=True,
        last_comment="Raised the pool. token=abc123secret " + "x" * 600,
    )
    tracker, _ = make_tracker(existing={f"{1:012x}": fixed})
    (item,) = plan_tickets([_issue(1)], settings, tracker)
    assert item.action is TicketAction.REGRESSION
    assert item.previous_fix is not None
    assert item.previous_fix.startswith("Raised the pool. token=[REDACTED:credential_pair]")
    assert "abc123secret" not in item.previous_fix
    assert len(item.previous_fix) == 500

    plain = ExistingTicket(key="SCRUM-2", url="https://t.example/browse/SCRUM-2", done=True)
    tracker, _ = make_tracker(existing={f"{1:012x}": plain})
    (item,) = plan_tickets([_issue(1)], settings, tracker)
    assert item.previous_fix is None
