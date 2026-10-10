"""Tests for incident_suite/state.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from incident_suite.config import AppConfig
from incident_suite.state import (
    AgentSettings,
    AgentUsage,
    Category,
    Issue,
    LogLevel,
    LogLine,
    Pattern,
    RedactionType,
    Remediation,
    RunResult,
    RunSettings,
    Severity,
)


def _issue(issue_id: str = "ISS-1", severity: Severity = Severity.HIGH) -> Issue:
    return Issue(
        id=issue_id,
        title="Database connection refused",
        category=Category.DATABASE,
        severity=severity,
        confidence=0.9,
        evidence_lines=[12, 13],
    )


def _remediation(issue_id: str = "ISS-1") -> Remediation:
    return Remediation(
        issue_id=issue_id,
        root_cause="Pool exhausted after the deploy at line 10.",
        steps=["Raise the pool size"],
        rationale="Errors start right after the deploy.",
        confidence=0.8,
    )


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "log_name": "payment.log",
        "log_fingerprint": "abc123",
        "log_format": "plain",
        "line_count": 100,
    }
    return RunResult.model_validate(fields | overrides)


# ------------------------------------------------------------------ RunSettings


def test_run_settings_from_config_defaults(config: AppConfig) -> None:
    settings = RunSettings.from_config(config)
    assert settings.log_reader.model == config.classifier_model
    assert settings.remediation.model == config.remediation_model
    assert settings.remediation.top_k == 3
    assert settings.jira.max_tickets == 3
    assert settings.redaction == frozenset(RedactionType)
    assert settings.live is False
    assert not settings.prompts_edited


def test_run_settings_round_trip_through_json(config: AppConfig) -> None:
    settings = RunSettings.from_config(config)
    assert RunSettings.model_validate_json(settings.model_dump_json()) == settings


def test_edited_prompt_is_detected(config: AppConfig) -> None:
    settings = RunSettings.from_config(config)
    edited = settings.model_copy(
        update={"cookbook": AgentSettings(model=config.classifier_model, system_prompt="Be terse.")}
    )
    assert edited.prompts_edited


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("remediation", "top_k"), 11),
        (("remediation", "top_k"), 0),
        (("remediation", "confidence_threshold"), 1.5),
        (("jira", "max_tickets"), 4),
        (("log_reader", "context_lines"), 50),
        (("log_reader", "model"), "someone/free-text-model"),
        (("token_budget",), 10_000_000),
        (("embedding_model",), "not/allowed"),
        (("unexpected_field",), True),
    ],
)
def test_run_settings_bounds_enforced_on_import(
    config: AppConfig, path: tuple[str, ...], value: object
) -> None:
    data = RunSettings.from_config(config).model_dump(mode="json")
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        RunSettings.model_validate(data)


def test_bad_json_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunSettings.model_validate_json("{not json")


# ------------------------------------------------------------------ log models


def test_log_line_defaults() -> None:
    line = LogLine(line_no=1, raw="hello", message="hello")
    assert line.level is LogLevel.UNKNOWN
    assert line.extra == {}


def test_log_line_numbers_start_at_one() -> None:
    with pytest.raises(ValidationError):
        LogLine(line_no=0, raw="x", message="x")


def test_pattern_rejects_reversed_times() -> None:
    with pytest.raises(ValidationError, match="first_seen"):
        Pattern(
            signature="Connection refused to db:<port>",
            signature_hash="a1b2c3d4",
            level=LogLevel.ERROR,
            count=2,
            first_seen=datetime(2026, 10, 10, 12, tzinfo=UTC),
            last_seen=datetime(2026, 10, 10, 11, tzinfo=UTC),
            example_line_nos=[1, 2],
        )


@pytest.mark.parametrize("bad_id", ["1", "ISS-", "iss-1", "ISS-12345"])
def test_issue_id_format(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _issue(issue_id=bad_id)


def test_issue_needs_evidence() -> None:
    with pytest.raises(ValidationError):
        Issue(
            id="ISS-1",
            title="No evidence",
            category=Category.OTHER,
            severity=Severity.LOW,
            confidence=0.5,
            evidence_lines=[],
        )


# ------------------------------------------------------------------ RunResult


def test_severity_rank_order() -> None:
    assert [s.rank for s in Severity] == [0, 1, 2, 3]


def test_top_severity_and_cost() -> None:
    result = _result(
        issues=[_issue("ISS-1", Severity.MEDIUM), _issue("ISS-2", Severity.CRITICAL)],
        usage=[
            AgentUsage(agent="log_reader", cost_usd=0.001),
            AgentUsage(agent="remediation", cost_usd=0.002),
        ],
    )
    assert result.top_severity is Severity.CRITICAL
    assert result.total_cost_usd == pytest.approx(0.003)


def test_empty_result() -> None:
    result = _result()
    assert result.top_severity is None
    assert result.total_cost_usd == 0
    assert result.dry_run


def test_remediation_must_match_an_issue() -> None:
    assert _result(issues=[_issue()], remediations=[_remediation()]).remediations
    with pytest.raises(ValidationError, match="ISS-9"):
        _result(issues=[_issue()], remediations=[_remediation("ISS-9")])
