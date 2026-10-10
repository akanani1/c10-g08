"""Agent settings: models, prompts and limits for this browser session only (plan section 17.1).

Nothing is written to disk, so one visitor can't change the prompts for everyone. The page only
has to produce a valid ``RunSettings``: the bounds live on the model, so a widget or an imported
file can't get around them. Edited prompts make runs dry unless the admin passcode is entered.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import streamlit as st
from pydantic import ValidationError

from incident_suite import ui
from incident_suite.config import (
    CHAT_MODELS,
    MAX_TICKETS_CEILING,
    RUN_TOKEN_BUDGET_CEILING,
    UPLOAD_CEILING_MB,
    AdminOverrides,
    AppConfig,
    admin_overrides,
    effective_config,
    load_config,
    set_admin_overrides,
)
from incident_suite.prompts import load_prompt
from incident_suite.security import passcode_ok
from incident_suite.state import (
    MAX_PROMPT_CHARS,
    AgentSettings,
    JiraSettings,
    LogReaderSettings,
    RedactionType,
    RemediationSettings,
    RunSettings,
    Severity,
)

MODELS = sorted(CHAT_MODELS)
AGENTS = ("log_reader", "remediation", "cookbook")
TITLES = {"log_reader": "Log Reader", "remediation": "Remediation", "cookbook": "Cookbook"}


# ------------------------------------------------------------------ widget state


def _defaults() -> RunSettings:
    return RunSettings.from_config(effective_config())


def _load_widgets(settings: RunSettings) -> None:
    """Put ``settings`` into the widgets. Runs in callbacks, before the widgets are drawn."""
    state = st.session_state
    for name in AGENTS:
        agent: AgentSettings = getattr(settings, name)
        state[f"model_{name}"] = agent.model
        state[f"prompt_{name}"] = agent.system_prompt or load_prompt(name)
    state.context_lines = settings.log_reader.context_lines
    state.top_k = settings.remediation.top_k
    state.threshold = settings.remediation.confidence_threshold
    state.max_tickets = settings.jira.max_tickets
    state.min_severity = settings.jira.min_severity
    state.token_budget = settings.token_budget
    state.require_approval = settings.require_approval
    state.redaction = sorted(settings.redaction)
    state.live = settings.live


def _current() -> RunSettings:
    if "run_settings" not in st.session_state:
        st.session_state.run_settings = _defaults()
    if "model_log_reader" not in st.session_state:
        _load_widgets(st.session_state.run_settings)
    settings: RunSettings = st.session_state.run_settings
    return settings


def _reset_prompt(name: str) -> None:
    st.session_state[f"prompt_{name}"] = load_prompt(name)


def _reset_all() -> None:
    st.session_state.run_settings = _defaults()
    _load_widgets(st.session_state.run_settings)
    st.session_state.settings_note = "Back to the default settings."


def _import() -> None:
    upload = st.session_state.get("import_file")
    if upload is None:
        return
    try:
        settings = ui.settings_from_json(upload.getvalue())
    except ValueError as exc:
        st.session_state.settings_note = str(exc)
        return
    st.session_state.run_settings = settings
    _load_widgets(settings)
    st.session_state.settings_note = "Imported. These settings now apply to your runs."


def _from_widgets() -> RunSettings:
    state = st.session_state
    prompts = {
        name: ui.prompt_override(state[f"prompt_{name}"], load_prompt(name)) for name in AGENTS
    }
    return RunSettings(
        log_reader=LogReaderSettings(
            model=state.model_log_reader,
            system_prompt=prompts["log_reader"],
            context_lines=state.context_lines,
        ),
        remediation=RemediationSettings(
            model=state.model_remediation,
            system_prompt=prompts["remediation"],
            top_k=state.top_k,
            confidence_threshold=state.threshold,
        ),
        cookbook=AgentSettings(model=state.model_cookbook, system_prompt=prompts["cookbook"]),
        jira=JiraSettings(max_tickets=state.max_tickets, min_severity=state.min_severity),
        embedding_model=state.run_settings.embedding_model,
        token_budget=state.token_budget,
        require_approval=state.require_approval,
        live=state.live,
        redaction=frozenset(state.redaction),
    )


def _apply() -> None:
    try:
        st.session_state.run_settings = _from_widgets()
    except ValidationError as exc:
        first = exc.errors()[0]
        st.session_state.settings_note = f"Not saved: {first['msg']}."
        return
    st.session_state.settings_note = "Saved. Your next run uses these settings."


# ------------------------------------------------------------------ cards


@contextmanager
def _agent_card(name: str) -> Iterator[None]:
    """A card with the agent's model on top and its prompt at the bottom."""
    with st.container(border=True):
        st.subheader(TITLES[name])
        st.selectbox("Model", MODELS, format_func=ui.model_label, key=f"model_{name}")
        yield
        with st.expander("System prompt"):
            st.text_area(
                "Prompt",
                key=f"prompt_{name}",
                height=260,
                max_chars=MAX_PROMPT_CHARS,
                label_visibility="collapsed",
            )
            st.button("Reset to default", key=f"reset_{name}", on_click=_reset_prompt, args=(name,))


def _integrations(config: AppConfig) -> None:
    with st.container(border=True):
        st.subheader("Integrations")
        for label, ready in (
            ("Models (OpenRouter)", config.llm_configured),
            ("Slack", config.slack_configured),
            ("JIRA", config.jira_configured),
            ("LangSmith", config.langsmith_tracing and config.langsmith_api_key is not None),
        ):
            st.write(f"{':green-badge[connected]' if ready else ':gray-badge[off]'} {label}")


def _cards(config: AppConfig) -> None:
    left, right = st.columns(2)
    with left:
        with _agent_card("log_reader"):
            st.slider("Context lines around each error", 0, 20, key="context_lines")
        with _agent_card("cookbook"):
            pass
        with st.container(border=True):
            st.subheader("JIRA")
            st.slider("Ticket cap per run", 0, 3, key="max_tickets")
            st.selectbox(
                "Severity that opens a ticket",
                [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM],
                key="min_severity",
            )
    with right:
        with _agent_card("remediation"):
            st.slider("Runbook matches (top-k)", 1, 10, key="top_k")
            st.slider(
                "Ask for more context below this confidence", 0.0, 1.0, step=0.05, key="threshold"
            )
        with st.container(border=True):
            st.subheader("Global")
            st.number_input(
                "Token budget per run",
                min_value=1_000,
                max_value=RUN_TOKEN_BUDGET_CEILING,
                step=10_000,
                key="token_budget",
            )
            st.toggle("Ask for approval before sending", key="require_approval")
            st.multiselect("Redact", list(RedactionType), key="redaction")
            st.toggle(
                "Send for real (live mode)",
                key="live",
                disabled=not config.live_integrations,
                help="Needs LIVE_INTEGRATIONS=true on this deployment.",
            )
        _integrations(config)


# ------------------------------------------------------------------ admin card

ADMIN_FIELDS = (
    "live_integrations",
    "require_approval",
    "max_upload_mb",
    "max_tickets_per_run",
    "run_token_budget",
    "jira_project_key",
    "jira_issue_type",
)


def _load_admin_widgets() -> None:
    config = effective_config()
    for name in ADMIN_FIELDS:
        value = getattr(config, name)
        st.session_state[f"admin_{name}"] = "" if value is None else value


def _save_admin() -> None:
    if not st.session_state.get("admin_ok"):
        st.session_state.admin_note = "Enter the admin passcode first."
        return
    values = {name: st.session_state[f"admin_{name}"] for name in ADMIN_FIELDS}
    values["jira_project_key"] = values["jira_project_key"].strip().upper() or None
    values["jira_issue_type"] = values["jira_issue_type"].strip() or None
    try:
        wanted = AdminOverrides(**values)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        st.session_state.admin_note = f"Not saved: {field}: {first['msg']}."
        return
    set_admin_overrides(AdminOverrides(**wanted.differences(load_config())))
    _load_admin_widgets()
    st.session_state.admin_note = "Saved for everyone until the app sleeps or restarts."


def _clear_admin() -> None:
    set_admin_overrides(AdminOverrides())
    _load_admin_widgets()
    st.session_state.admin_note = "Back to the deployment's own settings."


def _admin_card() -> None:
    """Deployment settings for every visitor. Needs the admin passcode (plan section 17.1)."""
    base = load_config()
    with st.container(border=True):
        st.subheader("Admin: settings for everyone")
        st.caption(
            "These start from the deployment's settings. A change applies to every visitor "
            "until the app sleeps, restarts or is redeployed, then the defaults come back. "
            "Keys, the JIRA site and account, and the Slack channel can't be changed here."
        )
        if base.admin_passcode is None:
            st.info("Set ADMIN_PASSCODE in the deployment's settings to use this card.")
            return
        if not st.session_state.get("admin_ok"):
            entered = st.text_input("Admin passcode", type="password", key="admin_entry")
            st.session_state.admin_ok = passcode_ok(entered, base.admin_passcode)
            if not st.session_state.admin_ok:
                return
        if "admin_live_integrations" not in st.session_state:
            _load_admin_widgets()
        if note := st.session_state.pop("admin_note", None):
            st.info(note)
        left, right = st.columns(2)
        with left:
            st.toggle("Live integrations (send to Slack and JIRA)", key="admin_live_integrations")
            st.toggle("Ask for approval before sending", key="admin_require_approval")
            st.slider(
                f"Upload limit in MB (at most {UPLOAD_CEILING_MB})",
                1,
                UPLOAD_CEILING_MB,
                key="admin_max_upload_mb",
            )
            st.slider("Tickets per run", 0, MAX_TICKETS_CEILING, key="admin_max_tickets_per_run")
        with right:
            st.number_input(
                "Token budget per run",
                min_value=1_000,
                max_value=RUN_TOKEN_BUDGET_CEILING,
                step=10_000,
                key="admin_run_token_budget",
            )
            st.text_input("JIRA project key", key="admin_jira_project_key", max_chars=20)
            st.text_input("JIRA issue type", key="admin_jira_issue_type", max_chars=50)
        if changed := admin_overrides().differences(base):
            st.caption("Changed from the defaults: " + ", ".join(sorted(changed)))
        save, clear, _ = st.columns([1, 1, 3])
        save.button("Save for everyone", type="primary", on_click=_save_admin)
        clear.button("Back to defaults", on_click=_clear_admin)


# ------------------------------------------------------------------ page


def main() -> None:
    """Render the settings page."""
    st.set_page_config(page_title="Agent settings", page_icon=":gear:", layout="wide")
    config = effective_config()
    settings = _current()
    st.title("Agent settings")
    st.caption(
        "Pick models, edit prompts and tune limits. Changes apply to your browser session only. "
        "Nothing is saved on the server. If a prompt differs from the default, runs are dry "
        "runs unless the admin passcode is entered on the main page."
    )
    if note := st.session_state.pop("settings_note", None):
        st.info(note)
    if settings.prompts_edited:
        st.warning("Your session uses edited prompts.")
    _cards(config)
    apply, reset, export, _ = st.columns([1, 1, 1, 2])
    apply.button("Save settings", type="primary", on_click=_apply)
    reset.button("Reset everything", on_click=_reset_all)
    export.download_button(
        "Export JSON",
        ui.settings_to_json(settings),
        file_name="incident-suite-settings.json",
        mime="application/json",
    )
    st.file_uploader("Import settings (JSON)", type=["json"], key="import_file", on_change=_import)
    _admin_card()


main()
