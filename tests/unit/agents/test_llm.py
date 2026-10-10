"""Tests for incident_suite/agents/__init__.py (model access). Nothing is sent."""

from __future__ import annotations

from typing import Any

import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from incident_suite.agents import (
    LLMError,
    MissingAPIKeyError,
    OpenRouterLLM,
    make_chat_model,
    make_embeddings,
)
from incident_suite.config import OPENROUTER_BASE_URL, AppConfig


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


class _Answer(BaseModel):
    verdict: str


class _FakeRunnable:
    """Stands in for ``ChatOpenAI.with_structured_output(...)``: replays queued results."""

    def __init__(self, results: list[dict[str, Any] | Exception]) -> None:
        self.results = results
        self.calls = 0

    def invoke(self, _messages: list[Any]) -> dict[str, Any]:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _raw(input_tokens: int, output_tokens: int) -> AIMessage:
    return AIMessage(
        content="{}",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def _patch(monkeypatch: pytest.MonkeyPatch, runnable: _FakeRunnable) -> None:
    monkeypatch.setattr(ChatOpenAI, "with_structured_output", lambda *_a, **_k: runnable)


def test_structured_call_returns_parsed_answer_and_usage(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    runnable = _FakeRunnable(
        [{"raw": _raw(120, 30), "parsed": _Answer(verdict="ok"), "parsing_error": None}]
    )
    _patch(monkeypatch, runnable)
    reply = OpenRouterLLM(config)(config.classifier_model, "system", "user", _Answer)
    assert reply.parsed.verdict == "ok"
    assert (reply.input_tokens, reply.output_tokens) == (120, 30)
    assert reply.model == config.classifier_model


def test_one_retry_on_a_malformed_answer(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    runnable = _FakeRunnable(
        [
            {"raw": _raw(100, 10), "parsed": None, "parsing_error": ValueError("bad")},
            {"raw": _raw(100, 20), "parsed": _Answer(verdict="ok"), "parsing_error": None},
        ]
    )
    _patch(monkeypatch, runnable)
    reply = OpenRouterLLM(config)(config.classifier_model, "s", "u", _Answer)
    assert runnable.calls == 2
    assert (reply.input_tokens, reply.output_tokens) == (200, 30)


def test_gives_up_after_two_malformed_answers(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = {"raw": _raw(100, 10), "parsed": None, "parsing_error": ValueError("bad")}
    _patch(monkeypatch, _FakeRunnable([bad, dict(bad)]))
    with pytest.raises(LLMError, match="after 2 tries") as caught:
        OpenRouterLLM(config)(config.classifier_model, "s", "u", _Answer)
    assert caught.value.input_tokens == 200


def test_api_errors_become_llm_errors(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeRunnable([openai.OpenAIError("connection reset")]))
    with pytest.raises(LLMError, match=r"failed \(OpenAIError\)"):
        OpenRouterLLM(config)(config.classifier_model, "s", "u", _Answer)
