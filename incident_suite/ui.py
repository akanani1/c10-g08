"""Display helpers for ``app.py``: escaping, badges, confidence reasons, the pipeline picture.

Everything here is a pure function of the run's data, so it can be tested without Streamlit.
Log text that reaches ``st.markdown`` must go through :func:`md` first. Tables
(``st.dataframe``) and code blocks show text as it is, so they don't need it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, timedelta
from typing import Any

from incident_suite.agents.jira_agent import TicketDraft
from incident_suite.agents.remediation import MIN_RUNBOOK_SCORE
from incident_suite.config import CHAT_MODELS, EMBEDDING_MODELS, AppConfig
from incident_suite.integrations.payloads import defang, jira_summary
from incident_suite.parsing.grouping import group_lines
from incident_suite.security import RUNS_PER_WINDOW, SHARED_RUNS_PER_HOUR, WINDOW_SECONDS
from incident_suite.state import (
    AgentUsage,
    Issue,
    LogFormat,
    LogLevel,
    LogLine,
    Pattern,
    RedactionType,
    Remediation,
    RunResult,
    RunSettings,
    RunStep,
    Severity,
    StepStatus,
    TicketAction,
    TicketOutcome,
)

Row = dict[str, Any]

MAX_TABLE_ROWS = 5_000
# Text formats count lines; structured formats (CSV, JSON, Parquet) count rows.
TEXT_FORMATS = {
    LogFormat.PLAIN,
    LogFormat.NGINX_ACCESS,
    LogFormat.NGINX_ERROR,
    LogFormat.SYSLOG,
    LogFormat.KUBERNETES,
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:=])")

SEVERITY_COLOURS = {
    Severity.CRITICAL: "red",
    Severity.HIGH: "orange",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "gray",
}
SEVERITY_LEGEND: list[Row] = [
    {
        "Severity": "Critical",
        "What happens": "JIRA ticket (up to the cap per run) and a Slack summary",
    },
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
    TicketAction.SKIPPED_OVER_CAP: "not ticketed (over the ticket cap)",
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
        if ticket.previous_fix:
            reasons.append(f"Last time it was fixed: {ticket.previous_fix[:120]}")
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


def pipeline_dot(
    steps: Sequence[RunStep], running: bool = False, current: str | None = None
) -> str:
    """The graph in Graphviz DOT, each node coloured by its status, for ``st.graphviz_chart``.

    ``current`` lights up one node as working, for the replay.
    """
    status = node_status(steps)
    working = {current} if current is not None else working_nodes(steps, running)
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


PROVIDER_NAMES = {"openai": "OpenAI", "google": "Google"}


def provider_of(model: str) -> str:
    """The model maker's display name, from the ``maker/model`` ID."""
    maker = model.split("/", 1)[0]
    return PROVIDER_NAMES.get(maker, maker)


def models_by_provider(models: Iterable[str]) -> dict[str, list[str]]:
    """Allowed models grouped by maker, for the Provider and Model dropdowns."""
    grouped: dict[str, list[str]] = {}
    for model in sorted(models):
        grouped.setdefault(provider_of(model), []).append(model)
    return grouped


def embedding_label(model: str) -> str:
    """An embedding model's price: they only charge for input."""
    return f"${EMBEDDING_MODELS[model].input_per_million:.2f} per M tokens (input only)"


def model_label(model: str) -> str:
    """A model and its price, such as ``openai/gpt-5-mini ($0.25 in / $2.00 out per M)``."""
    price = CHAT_MODELS[model]
    return (
        f"{model} (${price.input_per_million:.2f} in / "
        f"${price.output_per_million:.2f} out per M tokens)"
    )


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


# ------------------------------------------------------------------ postmortem


def _when(line: LogLine | None) -> str:
    if line is None or line.timestamp is None:
        return "time unknown"
    return f"{line.timestamp:%Y-%m-%d %H:%M:%S}"


def postmortem_markdown(result: RunResult, lines: Sequence[LogLine]) -> str:
    """A postmortem draft from the run's own results: no model call, nothing made up.

    Log text is defanged, so URLs from the log don't become links when the file is viewed.
    """
    by_number = {line.line_no: line for line in lines}
    structured = result.log_format not in TEXT_FORMATS
    fixes = {fix.issue_id: fix for fix in result.remediations}
    tickets = {t.issue_id: t for t in result.tickets}
    worst = result.top_severity.upper() if result.top_severity else "NONE"
    out = [
        f"# Postmortem draft: {defang(result.log_name)}",
        "",
        "> Draft built by Incident Suite from one log. Root causes are hypotheses. "
        "Review it before you share it.",
        "",
        "## Summary",
        "",
        f"- Run: {result.run_id} ({'dry run' if result.dry_run else 'live'})",
        f"- Worst severity: {worst}, {len(result.issues)} issue(s)",
        f"- Log: {result.line_count:,} lines, {len(result.patterns)} patterns, "
        f"fingerprint {result.log_fingerprint}",
    ]
    out += [
        f"- {t.key or t.related_key}: {t.action}{' (sent)' if t.sent else ''}"
        for t in result.tickets
        if t.key or t.related_key
    ]
    if result.slack.permalink:
        out.append(f"- Slack: {result.slack.permalink}")
    out += ["", "## Timeline", ""]
    events = [(line, f"Change: {defang(line.message)}") for line in group_lines(lines).change_lines]
    for issue in result.issues:
        seen = [by_number[n] for n in issue.evidence_lines if n in by_number]
        if seen:
            events.append((seen[0], f"{issue.id} first seen: {defang(issue.title)}"))
            events.append((seen[-1], f"{issue.id} last seen (L{seen[-1].line_no})"))
    events.sort(key=lambda event: event[0].line_no)
    out += [f"- {_when(line)} (L{line.line_no}): {text}" for line, text in events]
    if not events:
        out.append("- No timestamps or change events in the log.")
    out += ["", "## Issues, root causes and fixes", ""]
    for issue in sorted(result.issues, key=lambda i: -i.severity.rank):
        fix = fixes.get(issue.id)
        ticket = tickets.get(issue.id)
        out += [
            f"### {issue.id} [{issue.severity.upper()}] {defang(issue.title)}",
            "",
            f"- Service: {defang(issue.affected_service or 'unknown')}, category {issue.category}",
            f"- Evidence: {evidence_label(issue.evidence_lines, structured)}",
        ]
        if ticket is not None and (ticket.key or ticket.related_key):
            out.append(f"- JIRA: {ticket.key or ticket.related_key} ({ticket.action})")
        if fix is None:
            out += ["- No fix was suggested.", ""]
            continue
        out += [f"- Likely root cause (hypothesis): {defang(fix.root_cause)}", "", "Fix:"]
        out += [f"{n}. {defang(step)}" for n, step in enumerate(fix.steps, start=1)]
        sources = ", ".join(s.runbook for s in fix.sources) or "no runbook matched"
        out += ["", f"Runbooks: {sources}. Confidence {fix.confidence:.0%}.", ""]
    out += ["## Follow-ups", ""]
    follow_ups = [*result.checklist.this_week, *result.checklist.prevent]
    out += [f"- [ ] {defang(item.text)} ({', '.join(item.issue_ids)})" for item in follow_ups]
    if not follow_ups:
        out.append("- None suggested.")
    out += ["", f"Cost of this analysis: {money(result.total_cost_usd)}.", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ error timeline

# Status colours from the dataviz palette: serious for ERROR, critical for FATAL. WARN is left
# out: it's mostly noise, and its warning colour is too close to serious to tell apart.
TIMELINE_LEVELS = {LogLevel.ERROR: "#ec835a", LogLevel.FATAL: "#d03b3b"}
TIMELINE_BUCKETS_MINUTES = (1, 5, 15, 60, 240)
MAX_TIMELINE_BUCKETS = 180
CHANGE_LOOKBACK = timedelta(hours=1)
_MUTED = "#898781"
_GRID = "#2c2c2a"


def _bucket_minutes(span_minutes: float) -> int:
    for size in TIMELINE_BUCKETS_MINUTES:
        if span_minutes / size <= MAX_TIMELINE_BUCKETS:
            return size
    return TIMELINE_BUCKETS_MINUTES[-1]


def timeline_spec(lines: Sequence[LogLine]) -> tuple[dict[str, Any], int] | None:
    """A Vega-Lite chart of errors over time with change events marked, and its bucket size.

    ``None`` when the log has fewer than two timestamped errors, so there's nothing to plot.
    """
    errors = [
        (line.timestamp.astimezone(UTC), str(line.level))
        for line in lines
        if line.level in TIMELINE_LEVELS and line.timestamp is not None
    ]
    times = sorted({stamp for stamp, _ in errors})
    if len(times) < 2:
        return None
    # Changes shortly before the first error matter most: they're often the cause.
    earliest = times[0] - CHANGE_LOOKBACK
    changes = [
        (line.timestamp.astimezone(UTC), defang(line.message)[:120])
        for line in group_lines(lines).change_lines
        if line.timestamp is not None and earliest <= line.timestamp <= times[-1]
    ]
    first = min([times[0], *(stamp for stamp, _ in changes)])
    bucket = _bucket_minutes((times[-1] - first).total_seconds() / 60)
    counts: dict[tuple[str, str], int] = {}
    for stamp, level in errors:
        start = stamp.replace(second=0, microsecond=0)
        start -= timedelta(minutes=(start.hour * 60 + start.minute) % bucket)
        counts[(start.isoformat(), level)] = counts.get((start.isoformat(), level), 0) + 1
    bars = [{"time": t, "level": level, "count": n} for (t, level), n in sorted(counts.items())]
    marks = [{"time": stamp.isoformat(), "change": text} for stamp, text in changes]
    unit = {"unit": "utcyearmonthdatehoursminutes", "step": bucket}
    axis = {"labelColor": _MUTED, "titleColor": _MUTED, "gridColor": _GRID, "domainColor": _GRID}
    time_axis = {**axis, "format": "%H:%M" if bucket < 60 else "%d %b %H:%M", "labelOverlap": True}
    spec: dict[str, Any] = {
        "height": 220,
        "width": "container",
        "background": None,
        "layer": [
            {
                "data": {"values": bars},
                "mark": {"type": "bar", "stroke": "#0e1117", "strokeWidth": 1},
                "encoding": {
                    "x": {
                        "field": "time",
                        "timeUnit": unit,
                        "type": "temporal",
                        "title": None,
                        "axis": time_axis,
                        "scale": {"type": "utc"},
                    },
                    "y": {
                        "field": "count",
                        "aggregate": "sum",
                        "type": "quantitative",
                        "title": "Error lines",
                        "axis": axis,
                    },
                    "color": {
                        "field": "level",
                        "type": "nominal",
                        "scale": {
                            "domain": [str(level) for level in TIMELINE_LEVELS],
                            "range": list(TIMELINE_LEVELS.values()),
                        },
                        "legend": {"title": None, "orient": "top", "labelColor": _MUTED},
                    },
                    "tooltip": [
                        {
                            "field": "time",
                            "timeUnit": unit,
                            "type": "temporal",
                            "title": "From (UTC)",
                        },
                        {"field": "level", "type": "nominal", "title": "Level"},
                        {
                            "field": "count",
                            "aggregate": "sum",
                            "type": "quantitative",
                            "title": "Lines",
                        },
                    ],
                },
            },
            {
                "data": {"values": marks},
                "mark": {"type": "rule", "color": _MUTED, "strokeWidth": 2, "strokeDash": [4, 3]},
                "encoding": {
                    "x": {"field": "time", "type": "temporal", "scale": {"type": "utc"}},
                    "tooltip": [
                        {"field": "change", "type": "nominal", "title": "Change"},
                        {
                            "field": "time",
                            "type": "temporal",
                            "title": "At (UTC)",
                            "format": "%H:%M:%S",
                        },
                    ],
                },
            },
        ],
    }
    return spec, bucket


# ------------------------------------------------------------------ replay


def replay_frame(steps: Sequence[RunStep], position: int) -> tuple[str, str]:
    """The graph and a caption for step ``position`` (1-based) of a finished run.

    Earlier steps show their final status, the current one is lit up, later ones are waiting.
    The "more context" retry shows as the Log Reader lighting up again after Remediation.
    """
    position = max(1, min(position, len(steps)))
    step = steps[position - 1]
    label = LABELS.get(step.node, step.node)
    retry = " (the retry: more context)" if step.node == "log_reader" and position > 1 else ""
    caption = (
        f"Step {position} of {len(steps)}: {label}{retry}, {step.status}, "
        f"{step.seconds:.1f} s. {step.detail}"
    )
    return pipeline_dot(steps[: position - 1], current=step.node), caption


# ------------------------------------------------------------------ legend and redaction labels

REDACTION_LABELS = {
    RedactionType.EMAIL: "Email addresses",
    RedactionType.BEARER_TOKEN: "Bearer tokens (Authorization: Bearer ...)",
    RedactionType.JWT: "JWT login tokens (eyJ...)",
    RedactionType.AWS_ACCESS_KEY: "AWS access keys (AKIA...)",
    RedactionType.PRIVATE_KEY: "Private key blocks (-----BEGIN ... KEY-----)",
    RedactionType.CREDENTIAL_PAIR: "Values after password=, secret=, token=, api_key= and similar",
}


def legend_markdown() -> str:
    """The severity legend on one line: each badge and what it triggers."""
    return "  ".join(
        f"{severity_badge(severity)} {row['What happens'].lower()}"
        for severity, row in zip(
            (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW),
            SEVERITY_LEGEND,
            strict=True,
        )
    )


# ------------------------------------------------------------------ top bar and footer


def health_markdown(config: AppConfig, live: bool) -> str:
    """One row of badges: each integration connected or off, then the mode. Never the keys."""
    items = (
        ("OpenRouter", config.llm_configured),
        ("Slack", config.slack_configured),
        ("JIRA", config.jira_configured),
        ("LangSmith", config.langsmith_tracing and config.langsmith_api_key is not None),
    )
    on, off = ":green-badge[:material/check_circle: {}]", ":gray-badge[:material/cancel: {}]"
    badges = [(on if ready else off).format(label) for label, ready in items]
    mode = (
        ":orange-badge[:material/send: Live: approved runs post to Slack and JIRA]"
        if live
        else ":blue-badge[:material/visibility: Dry run: nothing is sent]"
    )
    return " ".join([*badges, mode])


def footer_text(config: AppConfig, version: str) -> str:
    """The version and the limits every run works within."""
    return (
        f"Incident Suite {version} · uploads up to {config.max_upload_mb} MB · "
        f"{RUNS_PER_WINDOW} runs per {WINDOW_SECONDS // 60} min per visitor, "
        f"{SHARED_RUNS_PER_HOUR} per hour in total · {config.run_token_budget:,} tokens per run · "
        "secrets are redacted before any AI sees the log"
    )
