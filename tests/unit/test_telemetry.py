"""Tests for incident_suite/telemetry.py."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from incident_suite.agents.log_reader import ClassifierAnswer
from incident_suite.llm import LLMError, LLMReply
from incident_suite.telemetry import UsageMeter, cost_usd, new_run_id


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
