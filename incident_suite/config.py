"""All settings in one place.

Values are read, lowest to highest priority, from: the defaults below, a local ``.env`` file,
process environment variables, and Streamlit secrets (on Streamlit Community Cloud). The rest
of the code only ever sees an :class:`AppConfig`, so it doesn't care where it is running.

Secrets are held as ``SecretStr``: they never show up in ``repr``, logs or the UI.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class ModelPrice(BaseModel):
    """Price of a model in US dollars per million tokens."""

    model_config = ConfigDict(frozen=True)

    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)


# Chat models anyone can pick (Agent settings page, env vars). OpenRouter list prices,
# checked 10 Oct 2026. Add a model here before using it, so the cost panel always knows its price.
CHAT_MODELS: dict[str, ModelPrice] = {
    "openai/gpt-4.1-mini": ModelPrice(input_per_million=0.40, output_per_million=1.60),
    "openai/gpt-5-mini": ModelPrice(input_per_million=0.25, output_per_million=2.00),
    "openai/gpt-5-nano": ModelPrice(input_per_million=0.05, output_per_million=0.40),
    "google/gemini-2.5-flash": ModelPrice(input_per_million=0.30, output_per_million=2.50),
}

# Embedding models: input tokens only.
EMBEDDING_MODELS: dict[str, ModelPrice] = {
    "openai/text-embedding-3-small": ModelPrice(input_per_million=0.02, output_per_million=0.0),
}

DEFAULT_CLASSIFIER_MODEL = "openai/gpt-4.1-mini"
DEFAULT_REMEDIATION_MODEL = "openai/gpt-5-mini"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"

# Hard ceilings. RunSettings enforces them too, so no widget or import can exceed them.
MAX_TICKETS_CEILING = 3
RUN_TOKEN_BUDGET_CEILING = 500_000


def _check_model(value: str, catalog: Mapping[str, ModelPrice], kind: str) -> str:
    if value not in catalog:
        allowed = ", ".join(sorted(catalog))
        msg = f"unknown {kind} model {value!r}; add it to config.py first (allowed: {allowed})"
        raise ValueError(msg)
    return value


def check_chat_model(value: str) -> str:
    """Return ``value`` if it is an allowed chat model, else raise ``ValueError``."""
    return _check_model(value, CHAT_MODELS, "chat")


def check_embedding_model(value: str) -> str:
    """Return ``value`` if it is an allowed embedding model, else raise ``ValueError``."""
    return _check_model(value, EMBEDDING_MODELS, "embedding")


class AppConfig(BaseModel):
    """Deployment settings. Field aliases are the environment variable names."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    # --- Models, through OpenRouter
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")
    classifier_model: str = Field(default=DEFAULT_CLASSIFIER_MODEL, alias="CLASSIFIER_MODEL")
    remediation_model: str = Field(default=DEFAULT_REMEDIATION_MODEL, alias="REMEDIATION_MODEL")
    embedding_model: str = Field(default=DEFAULT_EMBEDDING_MODEL, alias="EMBEDDING_MODEL")

    # Integrations
    live_integrations: bool = Field(default=False, alias="LIVE_INTEGRATIONS")
    require_approval: bool = Field(default=True, alias="REQUIRE_APPROVAL")
    slack_bot_token: SecretStr | None = Field(default=None, alias="SLACK_BOT_TOKEN")
    slack_channel_id: str | None = Field(default=None, alias="SLACK_CHANNEL_ID")
    jira_base_url: str | None = Field(default=None, alias="JIRA_BASE_URL")
    jira_email: str | None = Field(default=None, alias="JIRA_EMAIL")
    jira_api_token: SecretStr | None = Field(default=None, alias="JIRA_API_TOKEN")
    jira_project_key: str | None = Field(default=None, alias="JIRA_PROJECT_KEY")
    jira_issue_type: str = Field(default="Task", alias="JIRA_ISSUE_TYPE")

    # Tracing
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: SecretStr | None = Field(default=None, alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="c10-g08", alias="LANGSMITH_PROJECT")

    # Limits
    max_upload_mb: int = Field(default=5, ge=1, le=50, alias="MAX_UPLOAD_MB")
    max_tickets_per_run: int = Field(
        default=MAX_TICKETS_CEILING, ge=0, le=MAX_TICKETS_CEILING, alias="MAX_TICKETS_PER_RUN"
    )
    run_token_budget: int = Field(
        default=200_000, ge=1_000, le=RUN_TOKEN_BUDGET_CEILING, alias="RUN_TOKEN_BUDGET"
    )

    # Agent settings page: unlocks live sends after a prompt has been edited
    admin_passcode: SecretStr | None = Field(default=None, alias="ADMIN_PASSCODE")

    @field_validator("classifier_model", "remediation_model")
    @classmethod
    def _chat_model_allowed(cls, value: str) -> str:
        return check_chat_model(value)

    @field_validator("embedding_model")
    @classmethod
    def _embedding_model_allowed(cls, value: str) -> str:
        return check_embedding_model(value)

    @field_validator("jira_base_url")
    @classmethod
    def _jira_url_is_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("https://"):
            msg = "JIRA_BASE_URL must start with https://"
            raise ValueError(msg)
        return value.rstrip("/")

    @property
    def llm_configured(self) -> bool:
        """True when model calls can be made."""
        return self.openrouter_api_key is not None

    @property
    def slack_configured(self) -> bool:
        """True when every Slack credential is present."""
        return self.slack_bot_token is not None and bool(self.slack_channel_id)

    @property
    def jira_configured(self) -> bool:
        """True when every JIRA credential is present."""
        return all(
            (self.jira_base_url, self.jira_email, self.jira_api_token, self.jira_project_key)
        )


FIELD_ALIASES: frozenset[str] = frozenset(
    field.alias for field in AppConfig.model_fields.values() if field.alias
)


def _clean(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only known keys, and drop blanks so an empty ``KEY=`` means "use the default"."""
    return {
        key: value
        for key, value in values.items()
        if key in FIELD_ALIASES and value is not None and str(value).strip() != ""
    }


def _streamlit_secrets() -> dict[str, Any]:
    """Root-level Streamlit secrets, or nothing when there is no secrets file.

    A malformed secrets file still raises, so a broken deploy fails loudly.
    """
    try:
        return dict(st.secrets.to_dict())
    except FileNotFoundError:  # includes StreamlitSecretNotFoundError
        return {}


def load_config(
    env_file: Path | None = Path(".env"),
    environ: Mapping[str, str] | None = None,
    secrets: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Build the config from ``.env``, the environment and Streamlit secrets.

    The arguments exist so tests can pass their own sources; the app calls ``load_config()``.
    Pass ``env_file=None`` to skip the ``.env`` file.
    """
    merged: dict[str, Any] = {}
    if env_file is not None and env_file.is_file():
        merged |= _clean(dotenv_values(env_file))
    merged |= _clean(os.environ if environ is None else environ)
    merged |= _clean(_streamlit_secrets() if secrets is None else secrets)
    return AppConfig.model_validate(merged)
