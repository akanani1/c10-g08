"""The LangGraph run end to end, with a fake model, offline embeddings and fake Slack and JIRA.

Covers the routing (feedback loop, JIRA skipped when nothing is
critical) and the approval gate (dry run stops at the gate, reject sends nothing,
approve sends once, a paused run resumes with a rebuilt context).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.embeddings import Embeddings
from pydantic import SecretStr

from incident_suite import graph as graph_module
from incident_suite.agents import StructuredLLM
from incident_suite.agents.cookbook import CookbookAnswer, ItemDraft
from incident_suite.agents.jira_agent import TicketTracker
from incident_suite.agents.log_reader import ClassifierAnswer, IssueDraft
from incident_suite.agents.notification import Messenger
from incident_suite.agents.remediation import RemediationAnswer
from incident_suite.config import AppConfig
from incident_suite.graph import (
    RunContext,
    build_graph,
    connect_services,
    resume_run,
    start_run,
)
from incident_suite.integrations.jira_client import JiraClient
from incident_suite.integrations.slack_client import SlackClient
from incident_suite.parsing.readers import parse_upload
from incident_suite.rag.retriever import RunbookRetriever
from incident_suite.rag.runbooks import RUNBOOK_DIR, load_runbooks
from incident_suite.state import (
    ApprovalStatus,
    Category,
    NotificationPlan,
    ParsedLog,
    RunSettings,
    Severity,
    StepStatus,
    TicketAction,
)

MakeLLM = Callable[[Sequence[Any]], tuple[StructuredLLM, list[tuple[str, str, str]]]]
MakeTracker = Callable[..., tuple[TicketTracker, list[tuple[str, str]]]]
MakeMessenger = Callable[..., tuple[Messenger, list[NotificationPlan]]]
SAMPLE = Path(__file__).resolve().parents[2] / "sample_logs" / "payment_db_conn_refused.log"


@pytest.fixture(scope="module")
def parsed() -> ParsedLog:
    return parse_upload(SAMPLE.read_bytes(), SAMPLE.name, year=2026)


@pytest.fixture
def retriever(embeddings: Embeddings) -> RunbookRetriever:
    return RunbookRetriever(load_runbooks(RUNBOOK_DIR), embeddings)


def _classified(severity: Severity = Severity.CRITICAL) -> ClassifierAnswer:
    return ClassifierAnswer(
        issues=[
            IssueDraft(
                title="Payment DB connection pool exhausted after deploy",
                category=Category.DATABASE,
                severity=severity,
                confidence=0.9,
                pattern_ids=["P1", "P2"],
                evidence_lines=[],
                affected_service="payment-api",
            )
        ]
    )


def _fix(confidence: float = 0.85) -> RemediationAnswer:
    return RemediationAnswer(
        root_cause="Likely the deploy lowered db.pool.max_size to 5.",
        steps=["Roll back payment-api to v2.14.0", "Raise db.pool.max_size"],
        rationale="Errors start right after the deploy and config load.",
        source_ids=["R1"],
        confidence=confidence,
    )


CHECKLIST = CookbookAnswer(
    do_now=[ItemDraft(text="Roll back payment-api", issue_ids=["ISS-1"])],
    this_week=[],
    prevent=[ItemDraft(text="Alert on pool use above 80%", issue_ids=["ISS-1"])],
)


def _context(
    parsed: ParsedLog,
    settings: RunSettings,
    llm: StructuredLLM,
    retriever: RunbookRetriever,
    **services: Any,
) -> RunContext:
    return RunContext(parsed=parsed, settings=settings, llm=llm, retriever=retriever, **services)


def _live(settings: RunSettings, **changes: Any) -> RunSettings:
    return settings.model_copy(update={"live": True, **changes})


def test_dry_run_end_to_end(
    parsed: ParsedLog, settings: RunSettings, retriever: RunbookRetriever, make_llm: MakeLLM
) -> None:
    llm, calls = make_llm([_classified(), _fix(), CHECKLIST])
    result = start_run(build_graph(), _context(parsed, settings, llm, retriever), "run-1")

    assert len(calls) == 3
    (issue,) = result.issues
    assert issue.id == "ISS-1"
    assert issue.evidence_lines
    (fix,) = result.remediations
    assert fix.sources[0].runbook == "db-pool-exhausted"
    assert result.checklist.do_now[0].text == "Roll back payment-api"
    (ticket,) = result.tickets
    assert ticket.action is TicketAction.CREATE
    assert not ticket.sent
    assert result.notification is not None
    assert result.notification.post
    assert result.approval is ApprovalStatus.NOT_NEEDED
    assert result.dry_run
    assert [step.node for step in result.steps] == [
        "log_reader", "remediation", "cookbook", "jira_plan", "notification", "approval",
    ]  # fmt: skip
    assert result.steps[-1].status is StepStatus.SKIPPED
    assert {u.agent for u in result.usage} == {"log_reader", "remediation", "cookbook"}
    assert result.errors == []


def test_feedback_loop_retries_once(
    parsed: ParsedLog, settings: RunSettings, retriever: RunbookRetriever, make_llm: MakeLLM
) -> None:
    llm, calls = make_llm([_classified(), _fix(0.2), _fix(0.3), CHECKLIST])
    result = start_run(build_graph(), _context(parsed, settings, llm, retriever), "run-2")

    assert len(calls) == 4
    nodes = [step.node for step in result.steps]
    assert nodes[:4] == ["log_reader", "remediation", "log_reader", "remediation"]
    assert result.steps[2].detail == "More context for ISS-1"
    assert result.steps[1].detail == "1 issue(s); 1 sent back for more context"
    assert result.steps[3].detail == "1 issue(s); 0 sent back for more context"
    second_user_message = calls[2][2]
    assert second_user_message.count("\nL") > calls[1][2].count("\nL")  # wider evidence
    (remediation,) = [u for u in result.usage if u.agent == "remediation"]
    assert remediation.calls == 2


def test_jira_is_skipped_when_nothing_is_critical(
    parsed: ParsedLog, settings: RunSettings, retriever: RunbookRetriever, make_llm: MakeLLM
) -> None:
    llm, _ = make_llm([_classified(Severity.HIGH), _fix(), CHECKLIST])
    result = start_run(build_graph(), _context(parsed, settings, llm, retriever), "run-3")
    assert result.tickets == []
    skipped = [s for s in result.steps if s.node == "jira_plan"]
    assert skipped[0].status is StepStatus.SKIPPED


def test_healthy_log_makes_one_call(
    settings: RunSettings, retriever: RunbookRetriever, make_llm: MakeLLM
) -> None:
    healthy = parse_upload(b"2026-10-09T10:00:00Z INFO api GET /health 200\n", "ok.log")
    llm, calls = make_llm([])
    result = start_run(build_graph(), _context(healthy, settings, llm, retriever), "run-4")
    assert calls == []
    assert result.issues == []
    assert result.notification is not None
    assert not result.notification.post


def test_live_run_pauses_then_sends_once_on_approval(
    parsed: ParsedLog,
    settings: RunSettings,
    retriever: RunbookRetriever,
    make_llm: MakeLLM,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    llm, _ = make_llm([_classified(), _fix(), CHECKLIST])
    tracker, jira_calls = make_tracker()
    messenger, posts = make_messenger()
    graph = build_graph()
    services = {"tracker": tracker, "messenger": messenger}

    paused = start_run(graph, _context(parsed, _live(settings), llm, retriever, **services), "r5")
    assert paused.approval is ApprovalStatus.PENDING
    assert [method for method, _ in jira_calls] == ["find"]
    assert posts == []

    # A Streamlit rerun rebuilds the context; the graph and checkpointer are kept.
    rebuilt = _context(parsed, _live(settings), llm, retriever, **services)
    done = resume_run(graph, rebuilt, "r5", approved=True)

    assert done.approval is ApprovalStatus.APPROVED
    assert jira_calls[1:] == [
        ("create", "ISS-1<--"),
        ("remote_link", "OPS-101->https://slack.example/archives/C1/p1"),
    ]
    (ticket,) = done.tickets
    assert ticket.sent
    assert ticket.key == "OPS-101"
    assert len(posts) == 1
    assert done.slack.sent
    assert done.steps[-1].node == "send"


def test_rejected_run_sends_nothing(
    parsed: ParsedLog,
    settings: RunSettings,
    retriever: RunbookRetriever,
    make_llm: MakeLLM,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    llm, _ = make_llm([_classified(), _fix(), CHECKLIST])
    tracker, jira_calls = make_tracker()
    messenger, posts = make_messenger()
    context = _context(
        parsed, _live(settings), llm, retriever, tracker=tracker, messenger=messenger
    )
    graph = build_graph()
    start_run(graph, context, "r6")
    done = resume_run(graph, context, "r6", approved=False)

    assert done.approval is ApprovalStatus.REJECTED
    assert [method for method, _ in jira_calls] == ["find"]
    assert posts == []
    assert done.steps[-1].detail == "Rejected in app: nothing sent"


def test_no_approval_needed_sends_straight_away(
    parsed: ParsedLog,
    settings: RunSettings,
    retriever: RunbookRetriever,
    make_llm: MakeLLM,
    make_messenger: MakeMessenger,
) -> None:
    llm, _ = make_llm([_classified(Severity.HIGH), _fix(), CHECKLIST])
    messenger, posts = make_messenger(error="channel_not_found")
    context = _context(
        parsed, _live(settings, require_approval=False), llm, retriever, messenger=messenger
    )
    result = start_run(build_graph(), context, "r7")
    assert result.approval is ApprovalStatus.APPROVED
    assert len(posts) == 1
    assert result.errors == ["Slack: channel_not_found"]


def test_token_budget_stops_remediation(
    parsed: ParsedLog, settings: RunSettings, retriever: RunbookRetriever, make_llm: MakeLLM
) -> None:
    tight = settings.model_copy(update={"token_budget": 1_000})
    llm, calls = make_llm([_classified(), CHECKLIST])
    result = start_run(build_graph(), _context(parsed, tight, llm, retriever), "r8")
    assert len(calls) == 1
    assert result.remediations == []
    assert "Token budget reached; no fix for ISS-1." in result.errors


def test_command_line_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Any,
    embeddings: Embeddings,
    make_llm: MakeLLM,
) -> None:
    llm, _ = make_llm([_classified(), _fix(), CHECKLIST])
    monkeypatch.setattr("incident_suite.config.load_config", lambda: config)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    monkeypatch.setattr("incident_suite.agents.make_embeddings", lambda _config: embeddings)

    assert graph_module.main([str(SAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "ISS-1 [critical] Payment DB connection pool exhausted after deploy" in out
    assert "1. Roll back payment-api to v2.14.0" in out
    assert "Dry run: nothing was sent." in out


def test_command_line_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], config: Any
) -> None:
    from incident_suite.config import AppConfig  # noqa: PLC0415

    monkeypatch.setattr("incident_suite.config.load_config", AppConfig)
    assert graph_module.main([str(SAMPLE)]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err

    monkeypatch.setattr("incident_suite.config.load_config", lambda: config)
    assert graph_module.main(["/nonexistent/file.log"]) == 1
    assert "error:" in capsys.readouterr().err


# ------------------------------------------------------------------ real clients, mocked network

SITE = "https://team.atlassian.net"
PERMALINK = "https://team.slack.com/archives/C123/p1700000000000100"


def _live_config(config: AppConfig) -> AppConfig:
    return config.model_copy(
        update={
            "live_integrations": True,
            "slack_bot_token": SecretStr("xoxb-test-token"),
            "slack_channel_id": "C123",
            "jira_base_url": SITE,
            "jira_email": "bot@example.com",
            "jira_api_token": SecretStr("jira-test-token"),
            "jira_project_key": "OPS",
        }
    )


def _network(seen: list[str]) -> httpx.MockTransport:
    """Answers like Jira Cloud and Slack would. Records ``METHOD path`` for each call."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        replies = {
            "/rest/api/3/search/jql": {"issues": [], "isLast": True},
            "/rest/api/3/issue": {"id": "10001", "key": "OPS-101"},
            "/rest/api/3/issue/OPS-101/remotelink": {"id": 1},
            "/api/chat.postMessage": {"ok": True, "ts": "1700000000.000100"},
            "/api/chat.getPermalink": {"ok": True, "permalink": PERMALINK},
        }
        return httpx.Response(200, json=replies[request.url.path])

    return httpx.MockTransport(handler)


def test_connect_services(config: AppConfig, settings: RunSettings) -> None:
    live_config = _live_config(config)
    assert connect_services(live_config, settings) == (None, None)

    tracker, messenger = connect_services(live_config, _live(settings))
    assert isinstance(tracker, JiraClient)
    assert isinstance(messenger, SlackClient)

    slack_only = live_config.model_copy(update={"jira_api_token": None})
    tracker, messenger = connect_services(slack_only, _live(settings))
    assert tracker is None
    assert isinstance(messenger, SlackClient)


def test_live_run_through_the_real_clients(
    config: AppConfig,
    parsed: ParsedLog,
    settings: RunSettings,
    retriever: RunbookRetriever,
    make_llm: MakeLLM,
) -> None:
    """Section 6.4 in order: JIRA, Slack summary, thread, permalink, then the link back."""
    seen: list[str] = []
    live_config = _live_config(config)
    tracker = JiraClient(live_config, transport=_network(seen))
    messenger = SlackClient(live_config, transport=_network(seen))
    llm, _ = make_llm([_classified(), _fix(), CHECKLIST])
    context = _context(
        parsed, _live(settings), llm, retriever, tracker=tracker, messenger=messenger
    )
    graph = build_graph()

    start_run(graph, context, "r9")
    done = resume_run(graph, context, "r9", approved=True)

    assert seen == [
        "GET /rest/api/3/search/jql",
        "POST /rest/api/3/issue",
        "POST /api/chat.postMessage",
        "POST /api/chat.postMessage",
        "GET /api/chat.getPermalink",
        "POST /rest/api/3/issue/OPS-101/remotelink",
    ]
    (ticket,) = done.tickets
    assert ticket.key == "OPS-101"
    assert ticket.sent
    assert done.slack.permalink == PERMALINK
    assert done.errors == []


def test_send_reports_new_jira_errors_once(
    parsed: ParsedLog,
    settings: RunSettings,
    retriever: RunbookRetriever,
    make_llm: MakeLLM,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    llm, _ = make_llm([_classified(), _fix(), CHECKLIST])
    tracker, _ = make_tracker(fail="remote_link")
    messenger, _ = make_messenger()
    context = _context(
        parsed,
        _live(settings, require_approval=False),
        llm,
        retriever,
        tracker=tracker,
        messenger=messenger,
    )
    result = start_run(build_graph(), context, "r10")
    assert result.errors == [
        "JIRA (OPS-101): link to Slack: incident summary (r10) not added: JIRA returned 503"
    ]


def test_command_line_live_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: AppConfig,
    embeddings: Embeddings,
    make_llm: MakeLLM,
) -> None:
    seen: list[str] = []
    live_config = _live_config(config).model_copy(update={"jira_api_token": None})
    llm, _ = make_llm([_classified(Severity.HIGH), _fix(), CHECKLIST])
    monkeypatch.setattr("incident_suite.config.load_config", lambda: live_config)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    monkeypatch.setattr("incident_suite.agents.make_embeddings", lambda _config: embeddings)
    monkeypatch.setattr(
        graph_module,
        "connect_services",
        lambda cfg, _settings: (None, SlackClient(cfg, transport=_network(seen))),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    assert graph_module.main([str(SAMPLE), "--live"]) == 0
    out = capsys.readouterr().out
    assert "--- Slack summary ---" in out
    assert f"posted: {PERMALINK}" in out
    assert "Approval: approved." in out
    assert len(seen) == 3


def test_command_line_live_needs_the_switch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], config: AppConfig
) -> None:
    monkeypatch.setattr("incident_suite.config.load_config", lambda: config)
    assert graph_module.main([str(SAMPLE), "--live"]) == 1
    assert "LIVE_INTEGRATIONS=true" in capsys.readouterr().err
