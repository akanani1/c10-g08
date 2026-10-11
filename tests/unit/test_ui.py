"""Tests for incident_suite/ui.py: escaping, confidence reasons, the pipeline and the tables."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from incident_suite import ui
from incident_suite.config import AppConfig
from incident_suite.state import (
    AgentUsage,
    Category,
    Checklist,
    ChecklistItem,
    Issue,
    LogFormat,
    LogLevel,
    LogLine,
    Pattern,
    RedactionType,
    Remediation,
    RunbookSource,
    RunResult,
    RunSettings,
    RunStep,
    Severity,
    SlackOutcome,
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


def test_needs_runbook() -> None:
    assert ui.needs_runbook(None)
    assert ui.needs_runbook(FIX.model_copy(update={"sources": []}))
    weak = [RunbookSource(runbook="x", score=0.2)]
    assert ui.needs_runbook(FIX.model_copy(update={"sources": weak}))
    assert not ui.needs_runbook(FIX)


# ------------------------------------------------------------------ postmortem


def _when(minute: int) -> datetime:
    return datetime(2026, 10, 11, 3, minute, tzinfo=UTC)


def test_postmortem_from_a_run() -> None:
    lines = [
        LogLine(
            line_no=1,
            raw="deploy",
            message="deploy payment-api v2.15.0 started",
            timestamp=_when(0),
        ),
        LogLine(line_no=12, raw="err", message="Connection refused", timestamp=_when(5)),
        LogLine(line_no=13, raw="err", message="Connection refused", timestamp=_when(9)),
    ]
    issue = ISSUE.model_copy(update={"title": "DB pool exhausted, see http://evil.example"})
    result = RunResult(
        run_id="run-9",
        log_name="app.log",
        log_fingerprint="0" * 16,
        log_format=LogFormat.PLAIN,
        line_count=3,
        issues=[issue],
        remediations=[FIX],
        tickets=[
            TicketOutcome(issue_id="ISS-1", action=TicketAction.CREATE, key="SCRUM-7", sent=True)
        ],
        checklist=Checklist(prevent=[ChecklistItem(text="Alert on pool use", issue_ids=["ISS-1"])]),
        slack=SlackOutcome(sent=True, permalink="https://team.slack.com/archives/C1/p1"),
        dry_run=False,
    )
    text = ui.postmortem_markdown(result, lines)

    assert text.startswith("# Postmortem draft: app.log")
    assert "- Run: run-9 (live)" in text
    assert "- SCRUM-7: create (sent)" in text
    assert "- Slack: https://team.slack.com/archives/C1/p1" in text
    timeline = text.split("## Timeline")[1].split("##")[0]
    assert timeline.index("Change: deploy payment-api") < timeline.index("ISS-1 first seen")
    assert "2026-10-11 03:09:00 (L13): ISS-1 last seen (L13)" in timeline
    assert "http[:]//evil.example" in text
    assert "http://evil" not in text
    assert "- Evidence: lines 12, 13" in text
    assert "1. Roll back" in text
    assert "Runbooks: db_connection_pool, deploy_rollback. Confidence 85%." in text
    assert "- [ ] Alert on pool use (ISS-1)" in text


def test_postmortem_with_little_to_say() -> None:
    issue = ISSUE.model_copy(update={"evidence_lines": [4]})
    result = RunResult(
        run_id="run-10",
        log_name="t.csv",
        log_fingerprint="0" * 16,
        log_format=LogFormat.CSV,
        line_count=1,
        issues=[issue],
    )
    text = ui.postmortem_markdown(result, [LogLine(line_no=4, raw="x", message="x")])
    assert "- Evidence: rows 4" in text
    assert "time unknown (L4): ISS-1 first seen" in text
    assert "- No fix was suggested." in text
    assert "- None suggested." in text
    assert "- No timestamps or change events in the log." in ui.postmortem_markdown(
        result.model_copy(update={"issues": []}), []
    )


def test_previous_fix_is_a_confidence_reason() -> None:
    regression = TicketOutcome(
        issue_id="ISS-1",
        action=TicketAction.REGRESSION,
        related_key="SCRUM-2",
        previous_fix="Raised the pool to 50 and restarted payment-api",
    )
    reasons = ui.confidence_reasons(ISSUE, FIX, regression, dry_run=False)
    assert reasons[-1] == "Last time it was fixed: Raised the pool to 50 and restarted payment-api"


# ------------------------------------------------------------------ error timeline


def _line(n: int, when: datetime, level: LogLevel, message: str = "boom") -> LogLine:
    return LogLine(line_no=n, raw=message, message=message, level=level, timestamp=when)


def test_timeline_counts_errors_per_minute_and_marks_changes() -> None:
    start = datetime(2026, 10, 11, 3, 0, 15, tzinfo=UTC)
    lines = [
        _line(1, start, LogLevel.INFO, "deploy payment-api v2.15 see https://ci.example/1"),
        _line(2, start + timedelta(seconds=30), LogLevel.ERROR),
        _line(3, start + timedelta(seconds=40), LogLevel.ERROR),
        _line(4, start + timedelta(minutes=2), LogLevel.FATAL),
        _line(5, start + timedelta(minutes=3), LogLevel.WARN),  # warnings aren't plotted
        _line(6, start + timedelta(minutes=9), LogLevel.ERROR),
    ]
    timeline = ui.timeline_spec(lines)
    assert timeline is not None
    spec, bucket = timeline
    assert bucket == 1
    bars, changes = (layer["data"]["values"] for layer in spec["layer"])
    assert bars == [
        {"time": "2026-10-11T03:00:00+00:00", "level": "ERROR", "count": 2},
        {"time": "2026-10-11T03:02:00+00:00", "level": "FATAL", "count": 1},
        {"time": "2026-10-11T03:09:00+00:00", "level": "ERROR", "count": 1},
    ]
    (deploy,) = changes  # the deploy just before the errors is marked: it's often the cause
    assert deploy == {
        "time": "2026-10-11T03:00:15+00:00",
        "change": "deploy payment-api v2.15 see https[:]//ci.example/1",
    }
    long_before = start - timedelta(hours=3)
    early = [_line(9, long_before, LogLevel.INFO, "config reloaded"), *lines[1:]]
    early_spec, _ = ui.timeline_spec(early) or ({}, 0)
    assert early_spec["layer"][1]["data"]["values"] == []  # hours earlier: not shown
    color = spec["layer"][0]["encoding"]["color"]["scale"]
    assert color == {"domain": ["ERROR", "FATAL"], "range": ["#ec835a", "#d03b3b"]}


def test_timeline_buckets_grow_with_the_span() -> None:
    start = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    day = [_line(n, start + timedelta(minutes=17 * n), LogLevel.ERROR) for n in range(1, 86)]
    spec, bucket = ui.timeline_spec(day) or ({}, 0)
    assert bucket == 15
    week = [
        _line(1, start, LogLevel.ERROR),
        _line(2, start + timedelta(days=6, hours=5), LogLevel.ERROR),
    ]
    assert (ui.timeline_spec(week) or ({}, 0))[1] == 60
    fortnight = [
        _line(1, start, LogLevel.ERROR),
        _line(2, start + timedelta(days=12, hours=5), LogLevel.ERROR),
    ]
    spec, bucket = ui.timeline_spec(fortnight) or ({}, 0)
    assert bucket == 240
    assert spec["layer"][0]["data"]["values"][-1]["time"] == "2026-10-23T04:00:00+00:00"


def test_no_timeline_without_timestamped_errors() -> None:
    when = datetime(2026, 10, 11, tzinfo=UTC)
    assert ui.timeline_spec([]) is None
    assert ui.timeline_spec([_line(1, when, LogLevel.ERROR)]) is None
    undated = LogLine(line_no=2, raw="x", message="x", level=LogLevel.ERROR)
    assert ui.timeline_spec([_line(1, when, LogLevel.ERROR), undated]) is None


# ------------------------------------------------------------------ replay


def test_replay_frames() -> None:
    steps = [
        _step("log_reader"),
        _step("remediation"),
        RunStep(
            node="log_reader", status=StepStatus.DONE, seconds=0.2, detail="More context for ISS-1"
        ),
        _step("remediation"),
        _step("cookbook"),
    ]
    dot, caption = ui.replay_frame(steps, 1)
    assert caption == "Step 1 of 5: Log Reader, done, 1.0 s. log_reader done"
    assert 'log_reader [label="Log Reader", fillcolor="#c2410c"]' in dot  # lit up
    assert 'remediation [label="Remediation", fillcolor="#27272a"]' in dot  # not reached yet

    dot, caption = ui.replay_frame(steps, 3)
    assert caption.startswith("Step 3 of 5: Log Reader (the retry: more context), done")
    assert 'remediation [label="Remediation", fillcolor="#166534"]' in dot

    assert ui.replay_frame(steps, 99)[1].startswith("Step 5 of 5: Cookbook")
    assert ui.replay_frame(steps, 0)[1].startswith("Step 1 of 5")


def test_legend_and_redaction_labels() -> None:
    legend = ui.legend_markdown()
    assert legend.startswith(
        ":red-badge[CRITICAL] jira ticket (up to 3 per run) and a slack summary"
    )
    assert ":gray-badge[LOW] nothing is sent. results stay in the app" in legend
    assert set(ui.REDACTION_LABELS) == set(RedactionType)


def test_health_bar_and_footer(config: AppConfig) -> None:
    bar = ui.health_markdown(config, live=False)
    assert ":green-badge[:material/check_circle: OpenRouter]" in bar
    assert ":gray-badge[:material/cancel: JIRA]" in bar
    assert bar.endswith(":blue-badge[:material/visibility: Dry run: nothing is sent]")
    assert "Live: approved runs" in ui.health_markdown(config, live=True)
    assert "sk-or" not in bar  # never the key
    assert ui.footer_text(config, "0.1.0") == (
        "Incident Suite 0.1.0 · uploads up to 5 MB · 5 runs per 10 min per visitor, "
        "60 per hour in total · 200,000 tokens per run · secrets are redacted before any AI "
        "sees the log"
    )
