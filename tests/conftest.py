"""Shared fixtures and fakes. Nothing here needs a real key or network access."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Sequence

import pytest
from langchain_core.embeddings import Embeddings
from pydantic import BaseModel, SecretStr

from incident_suite.agents import LLMError, LLMReply, SchemaT, StructuredLLM
from incident_suite.config import AppConfig
from incident_suite.state import RunSettings

LLMCall = tuple[str, str, str]  # (model, system prompt, user message)
MakeLLM = Callable[[Sequence[BaseModel | LLMError]], tuple[StructuredLLM, list[LLMCall]]]


class _FakeLLM:
    """Returns the queued answers in order (or raises queued errors) and records each call."""

    def __init__(self, answers: Sequence[BaseModel | LLMError]) -> None:
        self.answers = list(answers)
        self.calls: list[LLMCall] = []

    def __call__(
        self, model: str, system: str, user: str, schema: type[SchemaT]
    ) -> LLMReply[SchemaT]:
        self.calls.append((model, system, user))
        answer = self.answers.pop(0)
        if isinstance(answer, LLMError):
            raise answer
        if not isinstance(answer, schema):
            msg = f"queued {type(answer).__name__}, agent asked for {schema.__name__}"
            raise TypeError(msg)
        return LLMReply(answer, model, input_tokens=1000, output_tokens=200, seconds=0.5)


class WordEmbeddings(Embeddings):
    """Bag-of-words vectors: texts that share words are similar. Deterministic and offline."""

    dims = 256

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in re.findall(r"[a-z]{3,}", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dims] += 1.0  # noqa: S324
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def config() -> AppConfig:
    """Defaults plus a fake OpenRouter key, so clients can be built without network access."""
    return AppConfig(openrouter_api_key=SecretStr("sk-or-test-not-a-real-key"))


@pytest.fixture
def settings(config: AppConfig) -> RunSettings:
    """Default run settings."""
    return RunSettings.from_config(config)


@pytest.fixture
def make_llm() -> MakeLLM:
    """``llm, calls = make_llm([answer, ...])``: a fake model and the list of calls it got."""

    def make(answers: Sequence[BaseModel | LLMError]) -> tuple[StructuredLLM, list[LLMCall]]:
        fake = _FakeLLM(answers)
        return fake, fake.calls

    return make


@pytest.fixture
def embeddings() -> Embeddings:
    """Offline embeddings where shared words mean similarity."""
    return WordEmbeddings()
