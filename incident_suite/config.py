"""All settings in one place.

Values are read, lowest to highest priority, from: the defaults below, a local ``.env`` file,
process environment variables, and Streamlit secrets (on Streamlit Community Cloud). The rest
of the code only ever sees an :class:`AppConfig`, so it doesn't care where it is running.

Secrets are held as ``SecretStr``: they never show up in ``repr``, logs or the UI.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

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
MAX_TICKETS_CEILING = 5
DEFAULT_TICKETS_PER_RUN = 3
RUN_TOKEN_BUDGET_CEILING = 500_000
# Streamlit refuses bigger uploads itself (server.maxUploadSize in .streamlit/config.toml).
UPLOAD_CEILING_MB = 5


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
    cookbook_model: str | None = Field(default=None, alias="COOKBOOK_MODEL")  # None: classifier's

    # Agent tuning
    context_lines: int = Field(default=3, ge=0, le=20, alias="CONTEXT_LINES")
    runbook_top_k: int = Field(default=3, ge=1, le=10, alias="RUNBOOK_TOP_K")
    retry_confidence: float = Field(default=0.5, ge=0.0, le=1.0, alias="RETRY_CONFIDENCE")
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
    max_upload_mb: int = Field(default=5, ge=1, le=UPLOAD_CEILING_MB, alias="MAX_UPLOAD_MB")
    max_tickets_per_run: int = Field(
        default=DEFAULT_TICKETS_PER_RUN, ge=1, le=MAX_TICKETS_CEILING, alias="MAX_TICKETS_PER_RUN"
    )
    run_token_budget: int = Field(
        default=200_000, ge=1_000, le=RUN_TOKEN_BUDGET_CEILING, alias="RUN_TOKEN_BUDGET"
    )

    # Unlocks live sending, live sends with edited prompts, and the Admin card
    admin_passcode: SecretStr | None = Field(default=None, alias="ADMIN_PASSCODE")

    @field_validator("classifier_model", "remediation_model")
    @classmethod
    def _chat_model_allowed(cls, value: str) -> str:
        return check_chat_model(value)

    @field_validator("cookbook_model")
    @classmethod
    def _cookbook_model_allowed(cls, value: str | None) -> str | None:
        return None if value is None else check_chat_model(value)

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


# ------------------------------------------------------------------ admin overrides

_JIRA_KEY = r"^[A-Z][A-Z0-9_]+$"
_SLACK_CHANNEL = r"^[A-Z0-9]{2,20}$"
_PROJECT_NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$"
# Settings that decide where a secret is sent, and the secret that must come with them.
_CREDENTIAL_GROUPS = (
    (("jira_base_url", "jira_email"), "jira_api_token", "the JIRA site or email", "JIRA API token"),
    (("slack_channel_id",), "slack_bot_token", "the Slack channel", "Slack bot token"),
)


class AdminOverrides(BaseModel):
    """Settings an admin changes from the Admin Console, for every visitor (plan section 17.1).

    ``None`` means "use the value from .env, the environment or Streamlit secrets". Overrides
    live in this process's memory only, so they last until the app sleeps, restarts or is
    redeployed. Keys typed in are kept as ``SecretStr`` and never shown back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Models and agents
    openrouter_api_key: SecretStr | None = None
    run_token_budget: int | None = Field(default=None, ge=1_000, le=RUN_TOKEN_BUDGET_CEILING)
    classifier_model: str | None = None
    remediation_model: str | None = None
    cookbook_model: str | None = None
    embedding_model: str | None = None
    context_lines: int | None = Field(default=None, ge=0, le=20)
    runbook_top_k: int | None = Field(default=None, ge=1, le=10)
    retry_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # The app
    live_integrations: bool | None = None
    require_approval: bool | None = None
    max_upload_mb: int | None = Field(default=None, ge=1, le=UPLOAD_CEILING_MB)
    max_tickets_per_run: int | None = Field(default=None, ge=1, le=MAX_TICKETS_CEILING)
    # Slack, JIRA and LangSmith
    slack_bot_token: SecretStr | None = None
    slack_channel_id: str | None = Field(default=None, pattern=_SLACK_CHANNEL)
    jira_base_url: str | None = None
    jira_email: str | None = Field(default=None, max_length=200)
    jira_api_token: SecretStr | None = None
    jira_project_key: str | None = Field(default=None, pattern=_JIRA_KEY)
    langsmith_tracing: bool | None = None
    langsmith_project: str | None = Field(default=None, pattern=_PROJECT_NAME)
    langsmith_api_key: SecretStr | None = None

    def apply(self, config: AppConfig) -> AppConfig:
        """``config`` with every override that is set, checked by the same rules as env values.

        Raises ``ValidationError`` if the result breaks a rule (an unknown model, a JIRA URL
        that isn't https, a limit out of range).
        """
        merged = config.model_dump() | self.model_dump(exclude_none=True)
        return AppConfig.model_validate(merged)

    def differences(self, config: AppConfig) -> dict[str, Any]:
        """The overrides that actually change ``config``, by field name."""
        return {
            name: value
            for name, value in self.model_dump(exclude_none=True).items()
            if getattr(config, name) != value
        }

    def credential_problems(self, base: AppConfig) -> list[str]:
        """Where a secret goes can only change together with that secret.

        Otherwise a changed JIRA URL would receive the deployment's own JIRA token.
        """
        changed = self.differences(base)
        return [
            f"Changing {what} needs the {secret_name} too."
            for fields, secret, what, secret_name in _CREDENTIAL_GROUPS
            if any(name in changed for name in fields) and getattr(self, secret) is None
        ]


_admin = AdminOverrides()


def admin_overrides() -> AdminOverrides:
    """The overrides in force for every visitor of this running app."""
    return _admin


def set_admin_overrides(overrides: AdminOverrides) -> None:
    """Replace the overrides for every visitor, until the app restarts."""
    global _admin  # noqa: PLW0603 - one shared value per process is the point
    _admin = overrides


def restore_admin_defaults(fields: Iterable[str]) -> None:
    """Drop the overrides for some settings, for every visitor. The rest stay."""
    names = list(fields)
    unknown = set(names) - set(AdminOverrides.model_fields)
    if unknown:
        msg = f"not admin settings: {sorted(unknown)}"
        raise ValueError(msg)
    set_admin_overrides(_admin.model_copy(update=dict.fromkeys(names)))


def effective_config() -> AppConfig:
    """The deployment's config with the admin's overrides on top. The app calls this."""
    return _admin.apply(load_config())


def save_admin_changes(changes: Mapping[str, Any]) -> list[str]:
    """Merge ``changes`` into the overrides for everyone, or say why they can't be saved.

    A value equal to the deployment's own is dropped, so the console only remembers real
    changes. A blank secret means "keep the current one". Nothing is stored unless every check
    passes. Returns the problems, or an empty list when it saved.
    """
    base = load_config()
    current = _admin.model_dump(exclude_none=True)
    for name, value in changes.items():
        if isinstance(value, SecretStr) and not value.get_secret_value():
            continue
        if value is None or value == getattr(base, name):
            current.pop(name, None)
        else:
            current[name] = value
    try:
        wanted = AdminOverrides(**current)
        problems = wanted.credential_problems(base)
        if not problems:
            wanted.apply(base)
    except ValidationError as exc:
        return [_plain_error(error) for error in exc.errors()]
    if problems:
        return problems
    set_admin_overrides(wanted)
    return []


def _plain_error(error: Any) -> str:
    field = ".".join(str(part) for part in error["loc"]) or "value"
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{field}: {message}"
