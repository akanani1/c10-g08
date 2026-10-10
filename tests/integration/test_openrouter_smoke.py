"""OpenRouter smoke test (build plan section 22.3): real chat, embedding and classifier calls.

Opt-in: marked ``live``, so the default test run deselects it, and it skips when no
OPENROUTER_API_KEY is set. Run it with ``pytest -m live``. Costs a fraction of a cent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_suite.agents import OpenRouterLLM, make_chat_model, make_embeddings
from incident_suite.agents.log_reader import classify
from incident_suite.config import AppConfig, load_config
from incident_suite.parsing.grouping import group_lines
from incident_suite.parsing.readers import parse_upload
from incident_suite.state import Category, RunSettings
from incident_suite.telemetry import UsageMeter

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_config() -> AppConfig:
    """The real config from .env / environment, or skip when there is no key."""
    config = load_config()
    if not config.llm_configured:
        pytest.skip("OPENROUTER_API_KEY not set")
    return config


@pytest.mark.parametrize("role", ["classifier_model", "remediation_model"])
def test_chat_call(live_config: AppConfig, role: str) -> None:
    llm = make_chat_model(live_config, getattr(live_config, role))
    reply = llm.invoke("Reply with the single word: pong")
    assert "pong" in str(reply.content).lower()


def test_embedding_call(live_config: AppConfig) -> None:
    vectors = make_embeddings(live_config).embed_documents(["disk full", "connection refused"])
    assert len(vectors) == 2
    assert len(vectors[0]) == len(vectors[1]) > 100


def test_classifier_on_the_example_log(live_config: AppConfig) -> None:
    """One real classification of the payment example log: structured output end to end."""
    example = Path(__file__).resolve().parents[2] / (
        "docs/templates/gulam/example_logs/payment_db_conn_refused.log"
    )
    parsed = parse_upload(example.read_bytes(), example.name, year=2026)
    settings = RunSettings.from_config(live_config)
    meter = UsageMeter("log_reader", settings.log_reader.model)
    result = classify(
        parsed, group_lines(parsed.lines), settings, OpenRouterLLM(live_config), meter
    )
    assert result.errors == []
    assert result.issues
    assert result.issues[0].category is Category.DATABASE
    assert meter.usage.cost_usd < 0.01
