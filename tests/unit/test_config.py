"""Tests for incident_suite/config.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from incident_suite.config import (
    CHAT_MODELS,
    DEFAULT_CLASSIFIER_MODEL,
    DEFAULT_REMEDIATION_MODEL,
    AdminOverrides,
    AppConfig,
    admin_overrides,
    effective_config,
    load_config,
    save_admin_changes,
    set_admin_overrides,
)


def _load(
    tmp_path: Path,
    dotenv: str = "",
    environ: dict[str, str] | None = None,
    secrets: dict[str, object] | None = None,
) -> AppConfig:
    env_file = tmp_path / ".env"
    env_file.write_text(dotenv, encoding="utf-8")
    return load_config(env_file=env_file, environ=environ or {}, secrets=secrets or {})


def test_defaults_without_any_source() -> None:
    config = load_config(env_file=None, environ={}, secrets={})
    assert config.openrouter_api_key is None
    assert config.classifier_model == DEFAULT_CLASSIFIER_MODEL
    assert config.remediation_model == DEFAULT_REMEDIATION_MODEL
    assert config.live_integrations is False
    assert config.require_approval is True
    assert config.max_tickets_per_run == 3
    assert not config.llm_configured
    assert not config.slack_configured
    assert not config.jira_configured


def test_default_models_are_in_the_allow_list() -> None:
    assert DEFAULT_CLASSIFIER_MODEL in CHAT_MODELS
    assert DEFAULT_REMEDIATION_MODEL in CHAT_MODELS


def test_missing_env_file_is_fine(tmp_path: Path) -> None:
    config = load_config(env_file=tmp_path / "nope.env", environ={}, secrets={})
    assert config.max_upload_mb == 5


def test_priority_secrets_over_environ_over_dotenv(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        dotenv="MAX_UPLOAD_MB=2\nRUN_TOKEN_BUDGET=5000\nJIRA_ISSUE_TYPE=Bug\n",
        environ={"MAX_UPLOAD_MB": "3", "RUN_TOKEN_BUDGET": "6000"},
        secrets={"MAX_UPLOAD_MB": 4},
    )
    assert config.max_upload_mb == 4
    assert config.run_token_budget == 6000
    assert config.jira_issue_type == "Bug"


def test_blank_values_fall_back_to_defaults(tmp_path: Path) -> None:
    config = _load(tmp_path, dotenv="OPENROUTER_API_KEY=\nCLASSIFIER_MODEL=  \n")
    assert config.openrouter_api_key is None
    assert config.classifier_model == DEFAULT_CLASSIFIER_MODEL


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    config = _load(tmp_path, environ={"PATH": "/usr/bin", "HOME": "/root"})
    assert not hasattr(config, "PATH")


def test_secrets_never_appear_in_repr(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        environ={"OPENROUTER_API_KEY": "sk-or-supersecret", "SLACK_BOT_TOKEN": "xoxb-supersecret"},
    )
    assert config.llm_configured
    assert "supersecret" not in repr(config)
    assert "supersecret" not in config.model_dump_json()


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_booleans_parse_from_text(tmp_path: Path, value: str) -> None:
    assert _load(tmp_path, environ={"LIVE_INTEGRATIONS": value}).live_integrations


def test_slack_needs_token_and_channel(tmp_path: Path) -> None:
    assert not _load(tmp_path, environ={"SLACK_BOT_TOKEN": "xoxb-1"}).slack_configured
    both = {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_CHANNEL_ID": "C123"}
    assert _load(tmp_path, environ=both).slack_configured


def test_jira_needs_all_four_values(tmp_path: Path) -> None:
    jira = {
        "JIRA_BASE_URL": "https://team.atlassian.net/",
        "JIRA_EMAIL": "bot@example.com",
        "JIRA_API_TOKEN": "token",
    }
    assert not _load(tmp_path, environ=jira).jira_configured
    config = _load(tmp_path, environ=jira | {"JIRA_PROJECT_KEY": "OPS"})
    assert config.jira_configured
    assert config.jira_base_url == "https://team.atlassian.net"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CLASSIFIER_MODEL", "someone/free-text-model"),
        ("EMBEDDING_MODEL", "openai/gpt-4.1-mini"),
        ("JIRA_BASE_URL", "http://team.atlassian.net"),
        ("MAX_TICKETS_PER_RUN", "6"),
        ("MAX_TICKETS_PER_RUN", "0"),
        ("COOKBOOK_MODEL", "someone/free-text-model"),
        ("RETRY_CONFIDENCE", "1.5"),
        ("RUN_TOKEN_BUDGET", "10000000"),
        ("MAX_UPLOAD_MB", "0"),
        ("MAX_UPLOAD_MB", "6"),
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, key: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _load(tmp_path, environ={key: value})


class _FakeSecrets:
    def __init__(self, values: dict[str, object] | None) -> None:
        self._values = values

    def to_dict(self) -> dict[str, object]:
        if self._values is None:
            msg = "no secrets.toml"
            raise FileNotFoundError(msg)
        return self._values


def test_streamlit_secrets_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "incident_suite.config.st.secrets", _FakeSecrets({"JIRA_PROJECT_KEY": "SEC"})
    )
    assert load_config(env_file=None, environ={}).jira_project_key == "SEC"


def test_no_streamlit_secrets_file_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_suite.config.st.secrets", _FakeSecrets(None))
    assert load_config(env_file=None, environ={}).jira_project_key is None


def test_admin_overrides_apply_on_top_of_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    base = AppConfig(jira_project_key="SCRUM", run_token_budget=200_000)
    monkeypatch.setattr("incident_suite.config.load_config", lambda: base)
    assert effective_config() == base

    wanted = AdminOverrides(live_integrations=True, max_upload_mb=2, jira_project_key="SCRUM")
    assert wanted.differences(base) == {"live_integrations": True, "max_upload_mb": 2}
    set_admin_overrides(wanted)
    assert admin_overrides() is wanted
    config = effective_config()
    assert config.live_integrations
    assert config.max_upload_mb == 2
    assert config.jira_project_key == "SCRUM"
    assert config.run_token_budget == 200_000  # untouched settings keep their defaults


@pytest.mark.parametrize(
    "bad",
    [
        {"max_upload_mb": 6},  # Streamlit's own cap is 5 MB
        {"max_tickets_per_run": 6},
        {"max_tickets_per_run": 0},
        {"run_token_budget": 10_000_000},
        {"jira_project_key": "scrum; drop"},
        {"slack_channel_id": "#general"},
        {"langsmith_project": "<script>"},
        {"jira_issue_type": "Bug"},  # fixed to Task: not an override at all
    ],
)
def test_admin_overrides_are_bounded(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AdminOverrides(**bad)


def test_apply_checks_like_the_env_does() -> None:
    base = AppConfig()
    with pytest.raises(ValidationError, match="unknown chat model"):
        AdminOverrides(remediation_model="someone/free-model").apply(base)
    with pytest.raises(ValidationError, match="must start with https"):
        AdminOverrides(jira_base_url="http://jira.example").apply(base)
    applied = AdminOverrides(cookbook_model="openai/gpt-5-nano", runbook_top_k=5).apply(base)
    assert (applied.cookbook_model, applied.runbook_top_k) == ("openai/gpt-5-nano", 5)


def test_where_a_secret_goes_changes_only_with_the_secret() -> None:
    base = AppConfig(jira_base_url="https://ours.atlassian.net", jira_project_key="SCRUM")
    redirected = AdminOverrides(jira_base_url="https://evil.example")
    assert redirected.credential_problems(base) == [
        "Changing the JIRA site or email needs the JIRA API token too."
    ]
    with_token = redirected.model_copy(update={"jira_api_token": SecretStr("their-token")})
    assert with_token.credential_problems(base) == []
    channel = AdminOverrides(slack_channel_id="C999")
    assert channel.credential_problems(base) == [
        "Changing the Slack channel needs the Slack bot token too."
    ]
    assert AdminOverrides(jira_project_key="OPS").credential_problems(base) == []


def test_save_admin_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    base = AppConfig(
        jira_base_url="https://ours.atlassian.net",
        jira_api_token=SecretStr("ours"),
        run_token_budget=200_000,
    )
    monkeypatch.setattr("incident_suite.config.load_config", lambda: base)

    assert save_admin_changes({"run_token_budget": 50_000, "live_integrations": True}) == []
    assert admin_overrides() == AdminOverrides(run_token_budget=50_000, live_integrations=True)

    # equal to the deployment's own value: dropped; a blank secret: keep the current one
    assert save_admin_changes({"run_token_budget": 200_000, "jira_api_token": SecretStr("")}) == []
    assert admin_overrides() == AdminOverrides(live_integrations=True)

    problems = save_admin_changes({"jira_base_url": "https://evil.example"})
    assert problems == ["Changing the JIRA site or email needs the JIRA API token too."]
    assert admin_overrides() == AdminOverrides(live_integrations=True)  # nothing stored

    assert save_admin_changes({"remediation_model": "someone/free-model"}) == [
        "remediation_model: unknown chat model 'someone/free-model'; add it to config.py first "
        "(allowed: google/gemini-2.5-flash, openai/gpt-4.1-mini, openai/gpt-5-mini, "
        "openai/gpt-5-nano)"
    ]
    assert save_admin_changes({"max_tickets_per_run": 9})[0].startswith("max_tickets_per_run:")
