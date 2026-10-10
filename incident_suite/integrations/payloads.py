"""Slack blocks and JIRA documents, built by pure functions so they're easy to test.

Two rules hold for everything here:

- **Log text is never markup.** Anything that came from a log (titles, fix steps, service
  names, the log's own name) goes into Slack as ``plain_text`` or escaped ``mrkdwn``, and into
  JIRA as plain ADF text nodes, so it can't mention ``@channel`` or fake a link.
- **URLs from logs are never links.** They are defanged (``https[:]//``, ``www[.]``) before they
  go anywhere. The only links we build point at our own tickets, Slack messages and traces.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from incident_suite.agents.jira_agent import TicketDraft
from incident_suite.state import (
    NotificationPlan,
    Pattern,
    Severity,
    TicketAction,
    TicketOutcome,
)

Json = dict[str, Any]

# Slack Block Kit limits: 150 characters in a header, 3,000 in a section, 50 blocks a message.
SLACK_HEADER_CHARS = 150
SLACK_SECTION_CHARS = 3_000
SLACK_MAX_SECTIONS = 45
JIRA_SUMMARY_CHARS = 255
MAX_EVIDENCE_LISTED = 30
MORE_IN_APP = "(truncated; the full text is in the app)"
TRUNCATED = "... " + MORE_IN_APP

_SCHEME = re.compile(r"\b([a-z][a-z0-9+.-]{1,15})://", re.IGNORECASE)
_WWW = re.compile(r"\bwww\.", re.IGNORECASE)
_TICKET_WORDS = {
    TicketAction.CREATE: "new ticket",
    TicketAction.COMMENT: "updated, seen again",
    TicketAction.REGRESSION: "regression",
}
_PRIORITY = {
    Severity.CRITICAL: "Highest",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
}


# ------------------------------------------------------------------ text safety


def defang(text: str) -> str:
    """Stop Slack and JIRA from turning a URL in log text into a link."""
    return _WWW.sub("www[.]", _SCHEME.sub(r"\1[:]//", text))


def slack_escape(text: str) -> str:
    """Make log text safe inside Slack ``mrkdwn``: no mentions, no links, no fake markup."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return defang(escaped)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(TRUNCATED)] + TRUNCATED


# ------------------------------------------------------------------ Slack


def _chunks(text: str, limit: int = SLACK_SECTION_CHARS) -> list[str]:
    """Split on line breaks into pieces Slack accepts, cutting single lines that are too long."""
    pieces: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = _cut(line, limit)
    if current:
        pieces.append(current)
    if len(pieces) > SLACK_MAX_SECTIONS:
        pieces = [*pieces[: SLACK_MAX_SECTIONS - 1], MORE_IN_APP]
    return pieces


def _plain_sections(text: str) -> list[Json]:
    return [
        {"type": "section", "text": {"type": "plain_text", "text": piece, "emoji": False}}
        for piece in _chunks(defang(text))
    ]


def _ticket_line(ticket: TicketOutcome) -> str | None:
    if ticket.action is TicketAction.SKIPPED_OVER_CAP:
        return None
    if ticket.error is not None and not ticket.sent:
        return f"{ticket.issue_id}: ticket failed ({slack_escape(ticket.error)})"
    if ticket.key is None or ticket.url is None:
        return None
    words = _TICKET_WORDS[ticket.action]
    if ticket.related_key is not None:
        words += f" of {ticket.related_key}"
    return f"{ticket.issue_id}: <{ticket.url}|{ticket.key}> {words}"


def slack_summary(plan: NotificationPlan, tickets: Sequence[TicketOutcome]) -> Json:
    """The channel message: a header, the summary as plain text, then links to the tickets."""
    first, _, rest = plan.summary.partition("\n")
    blocks: list[Json] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _cut(defang(first), SLACK_HEADER_CHARS)},
        },
        *_plain_sections(rest),
    ]
    lines = [line for line in map(_ticket_line, tickets) if line is not None]
    if lines:
        text = _cut("*JIRA*\n" + "\n".join(lines), SLACK_SECTION_CHARS)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    context = "Checklist and fixes in the thread."
    if plan.trace_url is not None:
        context += f" <{plan.trace_url}|Trace in LangSmith>"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    return {
        "text": slack_escape(first),
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def slack_thread(plan: NotificationPlan) -> Json:
    """The thread reply: the checklist, then each fix with its root cause, steps and sources."""
    return {
        "text": "Checklist and fixes",
        "blocks": _plain_sections(plan.thread),
        "unfurl_links": False,
        "unfurl_media": False,
    }


# ------------------------------------------------------------------ JIRA documents


def _text(value: str) -> Json:
    return {"type": "text", "text": defang(value)}


def _paragraph(value: str) -> Json:
    return {"type": "paragraph", "content": [_text(value)]}


def _heading(value: str) -> Json:
    return {"type": "heading", "attrs": {"level": 3}, "content": [_text(value)]}


def _list(kind: str, items: Sequence[str]) -> Json:
    return {
        "type": kind,
        "content": [{"type": "listItem", "content": [_paragraph(item)]} for item in items],
    }


def _doc(content: Sequence[Json]) -> Json:
    return {"type": "doc", "version": 1, "content": list(content)}


def _evidence(numbers: Sequence[int]) -> str:
    shown = ", ".join(f"L{n}" for n in numbers[:MAX_EVIDENCE_LISTED])
    extra = len(numbers) - MAX_EVIDENCE_LISTED
    return "Log lines " + shown + (f" and {extra} more" if extra > 0 else "")


def _seen(pattern: Pattern) -> str:
    window = ""
    if pattern.first_seen and pattern.last_seen:
        window = f" between {pattern.first_seen:%Y-%m-%d %H:%M:%S} and {pattern.last_seen:%H:%M:%S}"
    return f"Seen {pattern.count} times{window}."


def jira_summary(draft: TicketDraft) -> str:
    """``[CRITICAL] <issue title> (<service>)``, on one line and within JIRA's limit."""
    issue = draft.issue
    title = " ".join(issue.title.split())
    text = f"[{issue.severity.upper()}] {title} ({issue.affected_service or 'unknown service'})"
    return defang(text)[:JIRA_SUMMARY_CHARS]


def jira_priority(severity: Severity) -> str:
    """Highest for critical, High for high, and so on."""
    return _PRIORITY[severity]


def jira_description(draft: TicketDraft, regression_of: str | None) -> Json:
    """The ticket body: what happened, evidence, the fix and where it came from, and the run."""
    issue, fix = draft.issue, draft.remediation
    happened = (
        f"{issue.title}. Category {issue.category}, severity {issue.severity}, "
        f"service {issue.affected_service or 'unknown'}, confidence {issue.confidence:.0%}."
    )
    content = [_heading("What happened"), _paragraph(happened)]
    if draft.pattern is not None:
        pattern = draft.pattern
        content += [_paragraph(_seen(pattern)), _paragraph(f"Pattern: {pattern.signature}")]
    if regression_of is not None:
        content.append(_paragraph(f"Regression: this was seen before in {regression_of}."))
    content += [_heading("Evidence"), _paragraph(_evidence(issue.evidence_lines))]
    if fix is None:
        content.append(_paragraph("No fix was suggested for this issue. See the app."))
    else:
        sources = [f"{s.runbook} (match {s.score:.2f})" for s in fix.sources]
        content += [
            _heading("Likely root cause (a hypothesis)"),
            _paragraph(fix.root_cause),
            _heading("Fix steps"),
            _list("orderedList", fix.steps),
            _heading("Why"),
            _paragraph(fix.rationale),
            _heading("Runbook sources"),
            _list("bulletList", sources) if sources else _paragraph("No runbook matched."),
        ]
    content += [_heading("Run"), _paragraph(f"Opened by Incident Suite, run {draft.run_id}.")]
    return _doc(content)


def jira_comment(draft: TicketDraft) -> Json:
    """The comment on an open ticket: seen again, the run ID, the count and the time window."""
    issue = draft.issue
    lines = [f"Seen again in run {draft.run_id}."]
    if draft.pattern is not None:
        lines.append(_seen(draft.pattern))
    lines.append(f"{issue.title} ({issue.severity}). {_evidence(issue.evidence_lines)}.")
    return _doc([_paragraph(line) for line in lines])
