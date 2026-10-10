"""Tests for eval/run_eval.py: the scoring, with made-up results and no key."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from langchain_core.embeddings import Embeddings

from incident_suite.agents import StructuredLLM
from incident_suite.agents.cookbook import CookbookAnswer
from incident_suite.agents.log_reader import ClassifierAnswer, IssueDraft
from incident_suite.agents.remediation import RemediationAnswer
from incident_suite.config import AppConfig
from incident_suite.state import (
    AgentUsage,
    Category,
    Issue,
    LogFormat,
    NotificationPlan,
    Remediation,
    RunResult,
    Severity,
    TicketAction,
    TicketOutcome,
)

MakeLLM = Callable[[Sequence[Any]], tuple[StructuredLLM, list[tuple[str, str, str]]]]
SCRIPT = Path(__file__).resolve().parents[2] / "eval" / "run_eval.py"


@pytest.fixture(scope="module")
def run_eval() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


LABEL: dict[str, Any] = {
    "file": "mixed.json",
    "expect_ticket": True,
    "expect_slack": True,
    "expected_issues": [
        {
            "category": "database",
            "severity": "critical",
            "evidence_lines": [10, 11],
            "service": "payment-api",
            "expected_fix_keywords": ["pool"],
        },
        {
            "category": "timeout",
            "severity": "medium",
            "evidence_lines": [50],
            "service": "search",
            "signature_hint": "upstream timed out",
        },
        {"category": "tls", "severity": "high", "evidence_lines": [90], "service": "gateway"},
    ],
}


def _issue(n: int, category: Category, severity: Severity, lines: list[int], **extra: Any) -> Issue:
    return Issue.model_validate(
        {
            "id": f"ISS-{n}",
            "title": extra.pop("title", f"Problem {n}"),
            "category": category,
            "severity": severity,
            "confidence": 0.8,
            "evidence_lines": lines,
            **extra,
        }
    )


def _result(issues: list[Issue], **extra: Any) -> RunResult:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "log_name": "mixed.json",
        "log_fingerprint": "0" * 16,
        "log_format": LogFormat.JSON_DOCUMENT,
        "line_count": 100,
        "issues": issues,
        "usage": [
            AgentUsage(agent="log_reader", input_tokens=900, output_tokens=100, cost_usd=0.01)
        ],
    }
    return RunResult.model_validate(fields | extra)


def test_score_matches_by_lines_then_service_then_hint(run_eval: ModuleType) -> None:
    issues = [
        _issue(1, Category.DATABASE, Severity.CRITICAL, [11]),  # shares a line
        _issue(2, Category.TIMEOUT, Severity.HIGH, [70], title="Upstream timed out on /q"),
        _issue(3, Category.OTHER, Severity.LOW, [5], affected_service="noise"),
    ]
    fix = Remediation(
        issue_id="ISS-1",
        root_cause="Pool too small.",
        steps=["Raise the pool size"],
        rationale="r",
        confidence=0.8,
    )
    result = _result(
        issues,
        remediations=[fix],
        tickets=[TicketOutcome(issue_id="ISS-1", action=TicketAction.CREATE)],
        notification=NotificationPlan(post=True, reason="r"),
    )
    s = run_eval.score(LABEL, result, seconds=2.0)

    assert (s.expected, s.found) == (3, 2)
    assert (s.category_ok, s.severity_ok, s.fix_ok) == (2, 1, 1)
    assert s.ticket_planned
    assert s.slack_planned
    assert (s.tokens, s.cost_usd, s.lines) == (1000, 0.01, 100)


def test_service_match_and_skipped_tickets(run_eval: ModuleType) -> None:
    by_service = _issue(1, Category.TLS, Severity.HIGH, [1], affected_service="Gateway-1")
    result = _result(
        [by_service],
        tickets=[TicketOutcome(issue_id="ISS-1", action=TicketAction.SKIPPED_OVER_CAP)],
    )
    s = run_eval.score(LABEL, result, seconds=1.0)
    assert s.found == 1
    assert s.category_ok == 1
    assert not s.ticket_planned
    assert not s.slack_planned


def test_summary_and_markdown(run_eval: ModuleType) -> None:
    score = run_eval.SampleScore
    scores = [
        score("a.log", 2, 2, 2, 1, 1, True, True, True, True, 100, 5, 1000, 0.01, 2.0, 0),
        score("healthy.csv", 0, 0, 0, 0, 0, False, True, False, False, 50, 1, 200, 0.002, 1.0, 0),
        score(
            "large_50k_mixed.parquet", 6, 5, 4, 3, 2, True, False, True, True,
            50_000, 40, 30_000, 0.05, 20.0, 1,
        ),
    ]  # fmt: skip
    rows = dict(run_eval.summary(scores))
    assert rows["Issues found"] == "7/8 (88%)"
    assert rows["Category accuracy"] == "6/8 (75%)"
    assert rows["Critical issues correctly ticketed"] == "1/2 (50%)"
    assert rows["Tickets that shouldn't have been opened"] == "1"
    assert rows["Slack decision right"] == "3/3 (100%)"
    assert rows["Average per run: tokens / cost / time"] == "10,400 / $0.0207 / 7.7 s"
    assert rows["50k-row log: patterns / tokens / cost / time"] == (
        "50,000 rows to 40 patterns / 30,000 / $0.0500 / 20.0 s"
    )
    table = run_eval.markdown(scores)
    assert table.startswith("| Metric | Result |")
    assert "| `healthy.csv` | 0/0 | 0 | 0 | no / yes | 200 | $0.0020 |" in table
    assert run_eval.summary([])[0] == ("Issues found", "n/a")


def test_main_needs_a_key(
    run_eval: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("incident_suite.config.load_config", AppConfig)
    assert run_eval.main([]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_main_scores_a_sample(
    run_eval: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: AppConfig,
    embeddings: Embeddings,
    make_llm: MakeLLM,
) -> None:
    classified = ClassifierAnswer(
        issues=[
            IssueDraft(
                title="Payment DB connection pool exhausted",
                category=Category.DATABASE,
                severity=Severity.CRITICAL,
                confidence=0.9,
                pattern_ids=["P1"],
                evidence_lines=[],
                affected_service="payment-api",
            )
        ]
    )
    fix = RemediationAnswer(
        root_cause="Pool too small.",
        steps=["Raise the database pool size"],
        rationale="r",
        source_ids=["R1"],
        confidence=0.9,
    )
    llm, _ = make_llm([classified, fix, CookbookAnswer(do_now=[], this_week=[], prevent=[])])
    monkeypatch.setattr("incident_suite.config.load_config", lambda: config)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    monkeypatch.setattr("incident_suite.agents.make_embeddings", lambda _config: embeddings)

    assert run_eval.main(["--only", "payment_db_conn_refused.log"]) == 0
    out = capsys.readouterr().out
    assert "payment_db_conn_refused.log: 1/1 found" in out
    assert "| Category accuracy | 1/1 (100%) |" in out
