"""Model clients. Every chat and embedding call goes through OpenRouter (build plan section 22).

LangChain's OpenAI classes work against OpenRouter by changing the base URL, so agents use
these two factories and never set URLs or keys themselves.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

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


class MissingAPIKeyError(RuntimeError):
    """Raised when a model call is attempted without ``OPENROUTER_API_KEY``."""


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
