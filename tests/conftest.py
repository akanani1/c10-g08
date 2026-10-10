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
from incident_suite.agents.jira_agent import (
    ExistingTicket,
    TicketDraft,
    TicketTracker,
    TrackerError,
)
from incident_suite.agents.notification import Messenger
from incident_suite.config import AppConfig
from incident_suite.state import (
    NotificationPlan,
    RunSettings,
    SlackOutcome,
    TicketOutcome,
)

LLMCall = tuple[str, str, str]  # (model, system prompt, user message)
MakeLLM = Callable[[Sequence[BaseModel | LLMError]], tuple[StructuredLLM, list[LLMCall]]]
TrackerCall = tuple[str, str]  # (method, what it was called with)


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


class _FakeTracker:
    """In-memory JIRA. ``existing`` maps signature hashes to tickets; ``fail`` breaks a method."""

    def __init__(self, existing: dict[str, ExistingTicket], fail: str | None) -> None:
        self.existing = existing
        self.fail = fail
        self.calls: list[TrackerCall] = []
        self._next = 100

    def _maybe_fail(self, method: str) -> None:
        if self.fail == method:
            msg = "JIRA returned 503"
            raise TrackerError(msg)

    def find_latest(self, signature_hash: str) -> ExistingTicket | None:
        self.calls.append(("find", signature_hash))
        self._maybe_fail("find")
        return self.existing.get(signature_hash)

    def create(self, draft: TicketDraft, regression_of: str | None) -> tuple[str, str]:
        self.calls.append(("create", f"{draft.issue.id}<-{regression_of or '-'}"))
        self._maybe_fail("create")
        self._next += 1
        return f"OPS-{self._next}", f"https://team.atlassian.example/browse/OPS-{self._next}"

    def comment(self, key: str, draft: TicketDraft) -> None:  # noqa: ARG002
        self.calls.append(("comment", key))
        self._maybe_fail("comment")

    def link_regression(self, key: str, closed_key: str) -> None:
        self.calls.append(("link", f"{key}->{closed_key}"))
        self._maybe_fail("link")

    def add_remote_link(self, key: str, url: str, title: str) -> None:  # noqa: ARG002
        self.calls.append(("remote_link", f"{key}->{url}"))
        self._maybe_fail("remote_link")


class _FakeMessenger:
    """In-memory Slack: records every post."""

    def __init__(self, error: str | None) -> None:
        self.error = error
        self.posts: list[NotificationPlan] = []

    def post(self, plan: NotificationPlan, tickets: Sequence[TicketOutcome]) -> SlackOutcome:  # noqa: ARG002
        self.posts.append(plan)
        if self.error:
            return SlackOutcome(error=self.error)
        return SlackOutcome(sent=True, permalink="https://slack.example/archives/C1/p1")


MakeTracker = Callable[..., tuple[TicketTracker, list[TrackerCall]]]
MakeMessenger = Callable[..., tuple[Messenger, list[NotificationPlan]]]


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


@pytest.fixture
def make_tracker() -> MakeTracker:
    """``tracker, calls = make_tracker(existing={...}, fail="create")``."""

    def make(
        existing: dict[str, ExistingTicket] | None = None, fail: str | None = None
    ) -> tuple[TicketTracker, list[TrackerCall]]:
        fake = _FakeTracker(existing or {}, fail)
        return fake, fake.calls

    return make


@pytest.fixture
def make_messenger() -> MakeMessenger:
    """``messenger, posts = make_messenger(error=None)``."""

    def make(error: str | None = None) -> tuple[Messenger, list[NotificationPlan]]:
        fake = _FakeMessenger(error)
        return fake, fake.posts

    return make
