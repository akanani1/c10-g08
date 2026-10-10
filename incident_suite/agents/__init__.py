"""Model clients. Every chat and embedding call goes through OpenRouter.

LangChain's OpenAI classes work against OpenRouter by changing the base URL, so agents use
these factories and never set URLs or keys themselves. Agents talk to a :class:`StructuredLLM`:
the real one is :class:`OpenRouterLLM`, and tests pass a fake with the same call signature.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

import openai
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel, SecretStr

from incident_suite.config import (
    OPENROUTER_BASE_URL,
    AppConfig,
    check_chat_model,
    check_embedding_model,
)

REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 2
# OpenRouter uses these to attribute traffic to an app; they carry no secrets.
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/akanani1/c10-g08",
    "X-Title": "Incident Suite",
}


ATTEMPTS = 2  # one retry when the answer doesn't match the schema

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class MissingAPIKeyError(RuntimeError):
    """Raised when a model call is attempted without ``OPENROUTER_API_KEY``."""


class LLMError(RuntimeError):
    """A model call failed. Carries the tokens already spent, so the cost panel stays honest."""

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class LLMReply(Generic[SchemaT]):
    """A validated structured answer plus what it cost."""

    parsed: SchemaT
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float


class StructuredLLM(Protocol):
    """Anything that turns a system prompt and a user message into a validated schema object."""

    def __call__(
        self, model: str, system: str, user: str, schema: type[SchemaT]
    ) -> LLMReply[SchemaT]:
        """Ask ``model`` and return an instance of ``schema``, or raise :class:`LLMError`."""
        ...


def _api_key(config: AppConfig) -> SecretStr:
    if config.openrouter_api_key is None:
        msg = "OPENROUTER_API_KEY is not set; add it to .env or the Streamlit secrets"
        raise MissingAPIKeyError(msg)
    return config.openrouter_api_key


def make_chat_model(config: AppConfig, model: str) -> ChatOpenAI:
    """A chat model on OpenRouter. ``model`` must be in the allow-list in ``config.py``."""
    return ChatOpenAI(
        model_name=check_chat_model(model),
        openai_api_base=OPENROUTER_BASE_URL,
        openai_api_key=_api_key(config),
        temperature=0,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
        default_headers=OPENROUTER_HEADERS,
    )


def make_embeddings(config: AppConfig) -> OpenAIEmbeddings:
    """The embedding model on OpenRouter, sending plain text rather than OpenAI token IDs."""
    return OpenAIEmbeddings(
        model=check_embedding_model(config.embedding_model),
        openai_api_base=OPENROUTER_BASE_URL,
        openai_api_key=_api_key(config),
        check_embedding_ctx_length=False,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
        default_headers=OPENROUTER_HEADERS,
    )


class OpenRouterLLM:
    """Structured-output calls through OpenRouter, with one retry on a malformed answer."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._clients: dict[str, ChatOpenAI] = {}

    def __call__(
        self, model: str, system: str, user: str, schema: type[SchemaT]
    ) -> LLMReply[SchemaT]:
        """Ask ``model`` for an answer matching ``schema``."""
        if model not in self._clients:
            self._clients[model] = make_chat_model(self._config, model)
        runnable = self._clients[model].with_structured_output(
            schema, method="json_schema", include_raw=True
        )
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        input_tokens = output_tokens = 0
        started = time.perf_counter()
        for _ in range(ATTEMPTS):
            try:
                result = runnable.invoke(messages)
            except openai.OpenAIError as exc:
                msg = f"The model call failed ({type(exc).__name__})."
                raise LLMError(msg, input_tokens, output_tokens) from exc
            usage = getattr(result["raw"], "usage_metadata", None) or {}
            input_tokens += int(usage.get("input_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
            parsed = result.get("parsed")
            if isinstance(parsed, schema):
                return LLMReply(
                    parsed, model, input_tokens, output_tokens, time.perf_counter() - started
                )
        msg = f"The model's answer didn't match the expected format after {ATTEMPTS} tries."
        raise LLMError(msg, input_tokens, output_tokens)
