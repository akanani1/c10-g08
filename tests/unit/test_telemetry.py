"""Tests for incident_suite/telemetry.py."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from langsmith.utils import LangSmithConnectionError
from pydantic import SecretStr

from incident_suite.agents import LLMError, LLMReply
from incident_suite.agents.log_reader import ClassifierAnswer
from incident_suite.config import AppConfig
from incident_suite.state import AgentUsage
from incident_suite.telemetry import (
    UsageMeter,
    cost_usd,
    enable_tracing,
    merge_usage,
    new_run_id,
    trace_link,
)


def test_run_id_format_and_uniqueness() -> None:
    now = datetime(2026, 10, 10, 14, 15, 2, tzinfo=UTC)
    first, second = new_run_id(now), new_run_id(now)
    assert re.fullmatch(r"run-20261010-141502-[0-9a-f]{6}", first)
    assert first != second
    assert new_run_id().startswith("run-")


def test_cost_from_list_prices() -> None:
    assert cost_usd("openai/gpt-4.1-mini", 1_000_000, 1_000_000) == pytest.approx(2.00)
    assert cost_usd("openai/gpt-5-nano", 2_000, 0) == pytest.approx(0.0001)


def test_meter_adds_successes_and_failures() -> None:
    meter = UsageMeter("remediation", "openai/gpt-4.1-mini")
    meter.record(LLMReply(ClassifierAnswer(issues=[]), "openai/gpt-4.1-mini", 1_000, 500, 1.5))
    meter.record_failure(LLMError("boom", input_tokens=200, output_tokens=0), seconds=0.5)
    usage = meter.usage
    assert usage.agent == "remediation"
    assert usage.calls == 2
    assert (usage.input_tokens, usage.output_tokens) == (1_200, 500)
    assert usage.seconds == pytest.approx(2.0)
    assert usage.cost_usd == pytest.approx((1_200 * 0.40 + 500 * 1.60) / 1_000_000)
    assert meter.tokens == 1_700


def test_merge_usage_adds_up_per_agent() -> None:
    entries = [
        AgentUsage(agent="remediation", calls=2, input_tokens=100, output_tokens=10, cost_usd=0.1),
        AgentUsage(agent="log_reader", calls=1, input_tokens=50, seconds=1.0),
        AgentUsage(agent="remediation", calls=1, input_tokens=40, output_tokens=5, cost_usd=0.05),
    ]
    merged = {u.agent: u for u in merge_usage(entries)}
    assert list(merged) == ["remediation", "log_reader"]
    assert merged["remediation"].calls == 3
    assert merged["remediation"].input_tokens == 140
    assert merged["remediation"].cost_usd == pytest.approx(0.15)


def test_enable_tracing(config: AppConfig) -> None:
    environ: dict[str, str] = {}
    assert not enable_tracing(config, environ)
    assert environ == {}

    on = config.model_copy(
        update={"langsmith_tracing": True, "langsmith_api_key": SecretStr("lsv2-test")}
    )
    assert enable_tracing(on, environ)
    assert environ == {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "lsv2-test",
        "LANGSMITH_PROJECT": "c10-g08",
    }


class _FakeLangSmith:
    def __init__(self, url: str | None, fail: bool = False) -> None:
        self.url, self.fail = url, fail
        self.projects: list[str] = []

    def __call__(self, api_key: str) -> _FakeLangSmith:
        assert api_key == "lsv2-test"
        return self

    def create_project(self, name: str, upsert: bool) -> SimpleNamespace:
        assert upsert
        if self.fail:
            msg = "no route"
            raise LangSmithConnectionError(msg)
        self.projects.append(name)
        return SimpleNamespace(url=self.url)


def test_trace_link(config: AppConfig) -> None:
    trace_id = UUID("12345678-1234-5678-1234-567812345678")
    keyed = config.model_copy(update={"langsmith_api_key": SecretStr("lsv2-test")})
    project = "https://smith.langchain.com/o/t1/projects/p/p1"

    fake = _FakeLangSmith(project)
    assert trace_link(keyed, trace_id, fake) == f"{project}/r/{trace_id}?poll=true"
    assert fake.projects == ["c10-g08"]

    assert trace_link(config, trace_id, fake) is None  # no key
    assert trace_link(keyed, trace_id, _FakeLangSmith(None)) is None
    assert trace_link(keyed, trace_id, _FakeLangSmith(project, fail=True)) is None
