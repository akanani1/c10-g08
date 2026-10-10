"""JIRA Ticket agent.

Split in two, so nothing is written before a person approves:

- ``plan_tickets`` is read-only. For each issue at or above the ticket severity (critical by
  default), worst first and capped at three, it searches JIRA for the issue's signature label
  and decides: create a ticket, comment on the open one, or open a regression of a closed one.
- ``send_tickets`` carries out that plan after approval, and links each regression to the
  closed ticket it repeats.
- ``link_back`` runs after the Slack post and adds remote links (the Slack thread, later the
  trace) to every ticket this run touched, so you can get from JIRA back to Slack.

All three talk to JIRA through :class:`TicketTracker`; the REST client in
``integrations/jira_client.py`` implements it, and tests use a fake. With no tracker (dry run),
every candidate is planned as a new ticket.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from incident_suite.state import (
    Issue,
    Pattern,
    Remediation,
    RunSettings,
    TicketAction,
    TicketOutcome,
)


class TrackerError(RuntimeError):
    """JIRA couldn't be reached or refused the request. The message is safe to show."""


@dataclass(frozen=True)
class ExistingTicket:
    """The newest ticket carrying an issue's signature label."""

    key: str
    url: str
    done: bool


@dataclass(frozen=True)
class TicketDraft:
    """Everything a ticket or comment is written from."""

    issue: Issue
    remediation: Remediation | None
    pattern: Pattern | None
    run_id: str


class TicketTracker(Protocol):
    """What the agent needs from JIRA."""

    def find_latest(self, signature_hash: str) -> ExistingTicket | None:
        """The newest ticket labelled ``sig-<signature_hash>``, or ``None``."""
        ...

    def create(self, draft: TicketDraft, regression_of: str | None) -> tuple[str, str]:
        """Open a ticket and return its key and URL. ``regression_of`` adds the label."""
        ...

    def comment(self, key: str, draft: TicketDraft) -> None:
        """Note on an open ticket that the issue was seen again in this run."""
        ...

    def link_regression(self, key: str, closed_key: str) -> None:
        """Link a new regression ticket to the closed ticket it repeats."""
        ...

    def add_remote_link(self, key: str, url: str, title: str) -> None:
        """Add a link to something outside JIRA, such as the Slack thread."""
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
    drafts: Mapping[str, TicketDraft],
    tracker: TicketTracker,
) -> list[TicketOutcome]:
    """Carry out an approved plan. ``drafts`` is keyed by issue ID.

    A planned item that already failed is not retried. If a regression ticket is created but
    the link to the old ticket fails, the ticket still counts as sent and the error is kept.
    """
    done = []
    for item in plan:
        if item.action is TicketAction.SKIPPED_OVER_CAP or item.error is not None:
            done.append(item)
            continue
        draft = drafts[item.issue_id]
        try:
            if item.action is TicketAction.COMMENT and item.key is not None:
                tracker.comment(item.key, draft)
                done.append(item.model_copy(update={"sent": True}))
                continue
            key, url = tracker.create(draft, item.related_key)
        except TrackerError as exc:
            done.append(item.model_copy(update={"error": str(exc)}))
            continue
        sent = item.model_copy(update={"key": key, "url": url, "sent": True})
        if item.related_key is not None:
            try:
                tracker.link_regression(key, item.related_key)
            except TrackerError as exc:
                error = f"{key} created, but not linked to {item.related_key}: {exc}"
                sent = sent.model_copy(update={"error": error})
        done.append(sent)
    return done


def link_back(
    tickets: Sequence[TicketOutcome], links: Sequence[tuple[str, str]], tracker: TicketTracker
) -> list[str]:
    """Add each ``(url, title)`` as a remote link on every ticket this run sent.

    Returns the errors, one per failed link, so the run can show them and carry on.
    """
    errors = []
    for ticket in tickets:
        if not ticket.sent or ticket.key is None:
            continue
        for url, title in links:
            try:
                tracker.add_remote_link(ticket.key, url, title)
            except TrackerError as exc:
                errors.append(f"JIRA ({ticket.key}): link to {title} not added: {exc}")
    return errors
