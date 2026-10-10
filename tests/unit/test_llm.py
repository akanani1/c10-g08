"""Tests for incident_suite/llm.py. Builds clients only; nothing is sent."""

from __future__ import annotations

import pytest

from incident_suite.config import OPENROUTER_BASE_URL, AppConfig
from incident_suite.llm import MissingAPIKeyError, make_chat_model, make_embeddings


def test_chat_model_points_at_openrouter(config: AppConfig) -> None:
    llm = make_chat_model(config, config.classifier_model)
    assert llm.model_name == config.classifier_model
    assert llm.openai_api_base == OPENROUTER_BASE_URL
    assert llm.temperature == 0


def test_embeddings_send_plain_text(config: AppConfig) -> None:
    embeddings = make_embeddings(config)
    assert embeddings.model == config.embedding_model
    assert embeddings.openai_api_base == OPENROUTER_BASE_URL
    assert embeddings.check_embedding_ctx_length is False


def test_model_outside_allow_list_is_refused(config: AppConfig) -> None:
    with pytest.raises(ValueError, match="unknown chat model"):
        make_chat_model(config, "someone/free-text-model")


def test_missing_key_gives_a_clear_error() -> None:
    no_key = AppConfig()
    with pytest.raises(MissingAPIKeyError, match="OPENROUTER_API_KEY"):
        make_chat_model(no_key, no_key.classifier_model)
    with pytest.raises(MissingAPIKeyError):
        make_embeddings(no_key)
