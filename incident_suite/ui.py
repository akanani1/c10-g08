"""Display helpers for ``app.py``: escaping, badges, confidence reasons, the pipeline picture.

Everything here is a pure function of the run's data, so it can be tested without Streamlit.
Log text that reaches ``st.markdown`` must go through :func:`md` first. Tables
(``st.dataframe``) and code blocks show text as it is, so they don't need it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import ValidationError

from incident_suite.agents.jira_agent import TicketDraft
from incident_suite.agents.remediation import MIN_RUNBOOK_SCORE
from incident_suite.config import CHAT_MODELS
from incident_suite.integrations.payloads import defang, jira_summary
from incident_suite.state import (
    AgentUsage,
    Issue,
    LogLine,
    Pattern,
    Remediation,
    RunSettings,
    RunStep,
    Severity,
    StepStatus,
    TicketAction,
    TicketOutcome,
)

Row = dict[str, Any]

MAX_TABLE_ROWS = 5_000
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:=])")

SEVERITY_COLOURS = {
    Severity.CRITICAL: "red",
    Severity.HIGH: "orange",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "gray",
}
SEVERITY_LEGEND: list[Row] = [
    {"Severity": "Critical", "What happens": "JIRA ticket (up to 3 per run) and a Slack summary"},
    {"Severity": "High", "What happens": "Slack summary with the checklist in a thread"},
    {"Severity": "Medium", "What happens": "Slack summary with the checklist in a thread"},
    {"Severity": "Low", "What happens": "Nothing is sent. Results stay in the app"},
]

# The pipeline as the app draws it: node id, label, and the nodes that must finish first.
PIPELINE: list[tuple[str, str, tuple[str, ...]]] = [
    ("log_reader", "Log Reader", ()),
    ("remediation", "Remediation", ("log_reader",)),
    ("cookbook", "Cookbook", ("remediation",)),
    ("jira_plan", "JIRA plan", ("remediation",)),
    ("notification", "Notification plan", ("cookbook",)),
    ("approval", "Approval", ("notification",)),
    ("send", "Send", ("approval",)),
]
LABELS = {node: label for node, label, _ in PIPELINE} | {"assistant": "Assistant"}
WORKING = "working"
WAITING = "waiting"
_FILL = {
    StepStatus.DONE: "#166534",
    StepStatus.SKIPPED: "#3f3f46",
    StepStatus.FAILED: "#991b1b",
    WORKING: "#c2410c",
    WAITING: "#27272a",
}
_TICKET_WORDS = {
    TicketAction.CREATE: "new ticket",
    TicketAction.COMMENT: "comment on the open ticket",
    TicketAction.REGRESSION: "new ticket, marked as a regression",
    TicketAction.SKIPPED_OVER_CAP: "not ticketed (over the cap of 3)",
}


# ------------------------------------------------------------------ text


def md(text: str) -> str:
    """Log text made safe for ``st.markdown``: no formatting, no links, no HTML."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", defang(text)).replace("\n", "  \n")


def severity_badge(severity: Severity) -> str:
    """A coloured Streamlit markdown badge, such as ``:red-badge[CRITICAL]``."""
    return f":{SEVERITY_COLOURS[severity]}-badge[{severity.upper()}]"


def money(usd: float) -> str:
    """Dollars with enough places to show a fraction of a cent."""
    return f"${usd:.4f}" if usd < 1 else f"${usd:.2f}"


def evidence_label(numbers: Sequence[int], structured: bool) -> str:
    """``lines 12, 13`` for text logs and ``rows 12, 13`` for tables."""
    word = "rows" if structured else "lines"
    return f"{word} " + ", ".join(str(n) for n in numbers)


# ------------------------------------------------------------------ incident cards


def confidence(issue: Issue, fix: Remediation | None) -> float:
    """The fix's confidence when there is a fix, else the classifier's."""
    return fix.confidence if fix is not None else issue.confidence


def confidence_reasons(
    issue: Issue, fix: Remediation | None, ticket: TicketOutcome | None, dry_run: bool
) -> list[str]:
    """Why the confidence is what it is, from real signals only (plan section 9)."""
    count = len(issue.evidence_lines)
    reasons = [f"{count} evidence line{'s' if count != 1 else ''} support it"]
    if fix is None:
        reasons.append("No fix was suggested")
    elif fix.sources:
        best = max(fix.sources, key=lambda source: source.score)
        reasons.append(f"Best runbook match {best.score:.2f} ({best.runbook})")
    else:
        reasons.append("No runbook matched")
    if ticket is not None and ticket.action is TicketAction.COMMENT:
        reasons.append(f"Seen before: {ticket.key} is still open")
    elif ticket is not None and ticket.action is TicketAction.REGRESSION:
        reasons.append(f"Seen before: {ticket.related_key} was closed, so this is a regression")
    elif ticket is not None and not dry_run:
        reasons.append("Not seen before in JIRA")
    return reasons


def ticket_preview(ticket: TicketOutcome, issue: Issue, run_id: str) -> str:
    """One line saying what JIRA will get (or got) for an issue."""
    title = jira_summary(TicketDraft(issue=issue, remediation=None, pattern=None, run_id=run_id))
    words = _TICKET_WORDS[ticket.action]
    key = ticket.key or ticket.related_key
    target = f"{key}: " if key and ticket.action is not TicketAction.CREATE else ""
    state = " (sent)" if ticket.sent else ""
    error = f" (failed: {ticket.error})" if ticket.error else ""
    return f"{issue.id} -> {target}{words}{state}{error}. Title: {title}"


# ------------------------------------------------------------------ pipeline


def node_status(steps: Iterable[RunStep]) -> dict[str, StepStatus]:
    """The latest status of each pipeline node. The retry's "more context" counts as Log Reader."""
    status: dict[str, StepStatus] = {}
    for step in steps:
        status[step.node] = step.status
    return status


def working_nodes(steps: Sequence[RunStep], running: bool) -> set[str]:
    """Nodes that can be running now: not finished, and everything before them finished."""
    if not running:
        return set()
    finished = node_status(steps)
    return {
        node
        for node, _, before in PIPELINE
        if node not in finished and all(b in finished for b in before)
    }


def pipeline_dot(steps: Sequence[RunStep], running: bool = False) -> str:
    """The graph in Graphviz DOT, each node coloured by its status, for ``st.graphviz_chart``."""
    status = node_status(steps)
    working = working_nodes(steps, running)
    lines = [
        "digraph pipeline {",
        '  rankdir=LR; bgcolor="transparent";',
        '  node [shape=box, style="rounded,filled", fontname="sans-serif", fontcolor="white",'
        ' color="#52525b"];',
        '  edge [color="#a1a1aa", fontcolor="#a1a1aa", fontname="sans-serif", fontsize=10];',
        f'  upload [label="Upload + redact", fillcolor="{_FILL[StepStatus.DONE]}"];',
    ]
    for node, label, _ in PIPELINE:
        state: str = WORKING if node in working else status.get(node, WAITING)
        dashed = ', style="rounded,filled,dashed"' if state == StepStatus.SKIPPED else ""
        lines.append(f'  {node} [label="{label}", fillcolor="{_FILL[state]}"{dashed}];')
    lines += [
        "  upload -> log_reader -> remediation;",
        '  remediation -> remediation [label="more context"];',
        "  remediation -> cookbook; remediation -> jira_plan;",
        "  cookbook -> notification; jira_plan -> notification;",
        "  notification -> approval -> send;",
        "}",
    ]
    return "\n".join(lines)


def pipeline_strip(steps: Sequence[RunStep]) -> list[Row]:
    """One row per pipeline node: its status and total time, in pipeline order."""
    status = node_status(steps)
    seconds: dict[str, float] = {}
    for step in steps:
        seconds[step.node] = seconds.get(step.node, 0.0) + step.seconds
    return [
        {
            "Step": label,
            "Status": str(status.get(node, "not run")),
            "Seconds": round(seconds.get(node, 0.0), 1),
        }
        for node, label, _ in PIPELINE
    ]


# ------------------------------------------------------------------ tables


def run_log_rows(steps: Sequence[RunStep]) -> list[Row]:
    """The run log: every step in the order it happened."""
    return [
        {
            "#": n,
            "Agent": LABELS.get(step.node, step.node),
            "Status": str(step.status),
            "Seconds": round(step.seconds, 2),
            "What happened": step.detail,
        }
        for n, step in enumerate(steps, start=1)
    ]


def usage_rows(usage: Sequence[AgentUsage]) -> list[Row]:
    """Tokens, cost and time per agent, with a total row."""
    rows: list[Row] = [
        {
            "Agent": LABELS.get(item.agent, item.agent),
            "Model": item.model or "-",
            "Calls": item.calls,
            "Tokens in": item.input_tokens,
            "Tokens out": item.output_tokens,
            "Cost": money(item.cost_usd),
            "Seconds": round(item.seconds, 1),
        }
        for item in usage
    ]
    rows.append(
        {
            "Agent": "Total",
            "Model": "",
            "Calls": sum(item.calls for item in usage),
            "Tokens in": sum(item.input_tokens for item in usage),
            "Tokens out": sum(item.output_tokens for item in usage),
            "Cost": money(sum(item.cost_usd for item in usage)),
            "Seconds": round(sum(item.seconds for item in usage), 1),
        }
    )
    return rows


def pattern_rows(patterns: Sequence[Pattern]) -> list[Row]:
    """Repeated errors, biggest first."""
    return [
        {
            "Pattern": p.signature,
            "Count": p.count,
            "Level": str(p.level),
            "Service": p.service or "",
            "First seen": p.first_seen.isoformat() if p.first_seen else "",
            "Last seen": p.last_seen.isoformat() if p.last_seen else "",
            "Hash": p.signature_hash,
        }
        for p in sorted(patterns, key=lambda p: -p.count)
    ]


def line_rows(lines: Sequence[LogLine], limit: int = MAX_TABLE_ROWS) -> list[Row]:
    """The parsed fields of the first ``limit`` lines."""
    return [
        {
            "Line": line.line_no,
            "Time": line.timestamp.isoformat() if line.timestamp else "",
            "Level": str(line.level),
            "Service": line.service or line.host or "",
            "Message": line.message,
            "Code": line.error_code or "",
            "Trace": line.trace_id or "",
        }
        for line in lines[:limit]
    ]


def source_rows(fixes: Sequence[Remediation]) -> list[Row]:
    """Each runbook cited, its best match score, and the issues that used it."""
    best: dict[str, float] = {}
    cited_by: dict[str, list[str]] = {}
    for fix in fixes:
        for source in fix.sources:
            best[source.runbook] = max(best.get(source.runbook, 0.0), source.score)
            cited_by.setdefault(source.runbook, []).append(fix.issue_id)
    return [
        {"Runbook": name, "Best match": round(score, 2), "Used for": ", ".join(cited_by[name])}
        for name, score in sorted(best.items(), key=lambda item: -item[1])
    ]


# ------------------------------------------------------------------ settings page


def model_label(model: str) -> str:
    """A model and its price, such as ``openai/gpt-5-mini ($0.25 in / $2.00 out per M)``."""
    price = CHAT_MODELS[model]
    return (
        f"{model} (${price.input_per_million:.2f} in / "
        f"${price.output_per_million:.2f} out per M tokens)"
    )


def prompt_override(text: str, default: str) -> str | None:
    """``None`` when the text is the default prompt (ignoring outer whitespace), else the text."""
    return None if text.strip() == default.strip() else text


def settings_to_json(settings: RunSettings) -> str:
    """The session's settings as JSON for download."""
    return settings.model_dump_json(indent=2)


def settings_from_json(raw: bytes) -> RunSettings:
    """Settings from an uploaded JSON file, checked like everything else.

    Raises ``ValueError`` with a short message that names the first bad field.
    """
    try:
        return RunSettings.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "file"
        msg = f"That settings file isn't valid ({where}: {first['msg']})."
        raise ValueError(msg) from exc


def decide_live(settings: RunSettings, live_allowed: bool, admin_ok: bool) -> tuple[bool, str]:
    """Whether this run may send for real, and a note saying why not when it can't.

    Edited prompts can change what gets sent, so they force a dry run unless the admin
    passcode was entered (plan section 17.1).
    """
    if not settings.live or not live_allowed:
        return False, ""
    if settings.prompts_edited and not admin_ok:
        return (
            False,
            "Prompts were edited, so this run is a dry run. The admin passcode unlocks it.",
        )
    return True, ""


def needs_runbook(fix: Remediation | None) -> bool:
    """True when an issue ended up without a good runbook match (plan section 17.3)."""
    if fix is None or not fix.sources:
        return True
    return max(source.score for source in fix.sources) < MIN_RUNBOOK_SCORE
