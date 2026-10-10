"""OpenRouter smoke test (build plan section 22.3): one real chat call and one embedding call.

Opt-in: marked ``live``, so the default test run deselects it, and it skips when no
OPENROUTER_API_KEY is set. Run it with ``make smoke``. Costs a fraction of a cent.
"""

from __future__ import annotations

import pytest

from incident_suite.config import AppConfig, load_config
from incident_suite.llm import make_chat_model, make_embeddings

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
