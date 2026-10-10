"""JIRA Ticket agent (build plan sections 4.2, 4.3 and 18.3).

Split in two, so nothing is written before a person approves:

- ``plan_tickets`` is read-only. For each issue at or above the ticket severity (critical by
  default), worst first and capped at three, it searches JIRA for the issue's signature label
  and decides: create a ticket, comment on the open one, or open a regression of a closed one.
- ``send_tickets`` carries out that plan after approval.

Both talk to JIRA through :class:`TicketTracker`; the REST client in
``integrations/jira_client.py`` implements it, and tests use a fake. With no tracker (dry run),
every candidate is planned as a new ticket.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from incident_suite.state import Issue, Remediation, RunSettings, TicketAction, TicketOutcome


class TrackerError(RuntimeError):
    """JIRA couldn't be reached or refused the request. The message is safe to show."""


@dataclass(frozen=True)
class ExistingTicket:
    """The newest ticket carrying an issue's signature label."""

    key: str
    url: str
    done: bool


class TicketTracker(Protocol):
    """What the agent needs from JIRA."""

    def find_latest(self, signature_hash: str) -> ExistingTicket | None:
        """The newest ticket labelled ``sig-<signature_hash>``, or ``None``."""
        ...

    def create(
        self, issue: Issue, remediation: Remediation | None, run_id: str, regression_of: str | None
    ) -> tuple[str, str]:
        """Open a ticket for ``issue`` and return its key and URL."""
        ...

    def comment(self, key: str, issue: Issue, run_id: str) -> None:
        """Note on an open ticket that the issue was seen again in this run."""
        ...


def _candidates(issues: Sequence[Issue], settings: RunSettings) -> list[Issue]:
    threshold = settings.jira.min_severity.rank
    eligible = [issue for issue in issues if issue.severity.rank >= threshold]
    return sorted(eligible, key=lambda issue: -issue.severity.rank)


def plan_tickets(
    issues: Sequence[Issue], settings: RunSettings, tracker: TicketTracker | None
) -> list[TicketOutcome]:
    """Decide what to do in JIRA for each eligible issue. Writes nothing."""
    plan = []
    for position, issue in enumerate(_candidates(issues, settings)):
        if position >= settings.jira.max_tickets:
            plan.append(TicketOutcome(issue_id=issue.id, action=TicketAction.SKIPPED_OVER_CAP))
            continue
        try:
            existing = (
                tracker.find_latest(issue.signature_hash)
                if tracker is not None and issue.signature_hash
                else None
            )
        except TrackerError as exc:
            plan.append(
                TicketOutcome(issue_id=issue.id, action=TicketAction.CREATE, error=str(exc))
            )
            continue
        if existing is None:
            plan.append(TicketOutcome(issue_id=issue.id, action=TicketAction.CREATE))
        elif existing.done:
            plan.append(
                TicketOutcome(
                    issue_id=issue.id, action=TicketAction.REGRESSION, related_key=existing.key
                )
            )
        else:
            plan.append(
                TicketOutcome(
                    issue_id=issue.id,
                    action=TicketAction.COMMENT,
                    key=existing.key,
                    url=existing.url,
                )
            )
    return plan


def send_tickets(
    plan: Sequence[TicketOutcome],
    issues: Sequence[Issue],
    remediations: Sequence[Remediation],
    run_id: str,
    tracker: TicketTracker,
) -> list[TicketOutcome]:
    """Carry out an approved plan. A planned item that already failed is not retried."""
    by_issue = {issue.id: issue for issue in issues}
    fixes = {fix.issue_id: fix for fix in remediations}
    done = []
    for item in plan:
        if item.action is TicketAction.SKIPPED_OVER_CAP or item.error is not None:
            done.append(item)
            continue
        issue = by_issue[item.issue_id]
        try:
            if item.action is TicketAction.COMMENT and item.key is not None:
                tracker.comment(item.key, issue, run_id)
                done.append(item.model_copy(update={"sent": True}))
            else:
                key, url = tracker.create(issue, fixes.get(issue.id), run_id, item.related_key)
                done.append(item.model_copy(update={"key": key, "url": url, "sent": True}))
        except TrackerError as exc:
            done.append(item.model_copy(update={"error": str(exc)}))
    return done
