"""Notification agent (build plan sections 4.2 to 4.4 and 18.3).

``plan_notification`` decides whether the run is worth a Slack post (at least one medium issue)
and writes the summary and the thread reply. ``send_notification`` posts it after approval,
through a :class:`Messenger`; the Slack client in ``integrations/slack_client.py`` implements it
and turns the plan into Slack blocks, escaping every piece of log text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from incident_suite.state import (
    Checklist,
    Issue,
    NotificationPlan,
    Remediation,
    Severity,
    SlackOutcome,
    TicketAction,
    TicketOutcome,
)

TOP_ISSUES = 5
MAX_EVIDENCE_SHOWN = 5
_TICKET_WORDS = {
    TicketAction.CREATE: "new ticket",
    TicketAction.COMMENT: "updated, seen again",
    TicketAction.REGRESSION: "regression",
    TicketAction.SKIPPED_OVER_CAP: "not ticketed (over the cap of 3)",
}


class Messenger(Protocol):
    """What the agent needs from Slack."""

    def post(self, plan: NotificationPlan, tickets: Sequence[TicketOutcome]) -> SlackOutcome:
        """Post the summary, reply with the thread, and return the outcome with its link."""
        ...


def _lines(numbers: Sequence[int]) -> str:
    shown = ", ".join(f"L{n}" for n in numbers[:MAX_EVIDENCE_SHOWN])
    extra = len(numbers) - MAX_EVIDENCE_SHOWN
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def _ticket_note(ticket: TicketOutcome | None) -> str:
    if ticket is None:
        return ""
    label = ticket.key or ticket.related_key or "JIRA"
    return f" | {label}: {_TICKET_WORDS[ticket.action]}"


def _summary(
    issues: Sequence[Issue],
    fixes: dict[str, Remediation],
    tickets: dict[str, TicketOutcome],
    log_name: str,
    run_id: str,
) -> str:
    worst = max(issues, key=lambda issue: issue.severity.rank).severity
    lines = [f"[{worst.upper()}] {len(issues)} issue(s) in {log_name} (run {run_id})"]
    for issue in issues[:TOP_ISSUES]:
        fix = fixes.get(issue.id)
        first_step = f" Fix: {fix.steps[0]}" if fix else ""
        ticket = _ticket_note(tickets.get(issue.id))
        lines.append(
            f"- {issue.id} [{issue.severity}] {issue.title} ({issue.affected_service or '-'}), "
            f"evidence {_lines(issue.evidence_lines)}.{first_step}{ticket}"
        )
    if len(issues) > TOP_ISSUES:
        lines.append(f"+{len(issues) - TOP_ISSUES} more in the app")
    return "\n".join(lines)


def _thread(issues: Sequence[Issue], fixes: dict[str, Remediation], checklist: Checklist) -> str:
    parts = []
    for heading, items in (
        ("Do now", checklist.do_now),
        ("This week", checklist.this_week),
        ("Prevent", checklist.prevent),
    ):
        if items:
            parts.append(
                heading
                + ":\n"
                + "\n".join(f"- {item.text} ({', '.join(item.issue_ids)})" for item in items)
            )
    for issue in issues:
        fix = fixes.get(issue.id)
        if fix is None:
            continue
        steps = "\n".join(f"{n}. {step}" for n, step in enumerate(fix.steps, start=1))
        sources = ", ".join(source.runbook for source in fix.sources) or "no runbook"
        parts.append(
            f"{issue.id} {issue.title}\nRoot cause: {fix.root_cause}\n{steps}\nSources: {sources}"
        )
    return "\n\n".join(parts)


def plan_notification(
    issues: Sequence[Issue],
    remediations: Sequence[Remediation],
    checklist: Checklist,
    tickets: Sequence[TicketOutcome],
    log_name: str,
    run_id: str,
) -> NotificationPlan:
    """What to post to Slack, or why nothing should be posted. Writes nothing."""
    if not issues:
        return NotificationPlan(post=False, reason="No issues found; results stay in the app.")
    if max(issue.severity.rank for issue in issues) < Severity.MEDIUM.rank:
        return NotificationPlan(
            post=False, reason="Only low-severity issues; results stay in the app."
        )
    fixes = {fix.issue_id: fix for fix in remediations}
    by_issue = {ticket.issue_id: ticket for ticket in tickets}
    return NotificationPlan(
        post=True,
        reason="At least one medium or worse issue.",
        summary=_summary(issues, fixes, by_issue, log_name, run_id),
        thread=_thread(issues, fixes, checklist),
    )


def send_notification(
    plan: NotificationPlan, tickets: Sequence[TicketOutcome], messenger: Messenger
) -> SlackOutcome:
    """Post an approved plan. Nothing to post means nothing is sent."""
    if not plan.post:
        return SlackOutcome()
    return messenger.post(plan, tickets)
