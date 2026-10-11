"""Streamlit entry point. UI only: all logic lives in the ``incident_suite`` package.

The compiled graph and the run's context are kept in ``st.session_state``, so a paused run
survives the reruns Streamlit does on every click, and Approve resumes the same run.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import openai
import streamlit as st
from pydantic import SecretStr

from incident_suite import __version__, ui
from incident_suite.agents import OpenRouterLLM, make_embeddings
from incident_suite.agents.assistant import (
    DRAFT_LABEL,
    MAX_QUESTION_CHARS,
    AssistantInput,
    AssistantResult,
    DraftResult,
    ReaderError,
    RecentTicket,
    ask_about_run,
    draft_runbook,
    latest_updates,
)
from incident_suite.config import (
    CHAT_MODELS,
    EMBEDDING_MODELS,
    MAX_TICKETS_CEILING,
    RUN_TOKEN_BUDGET_CEILING,
    UPLOAD_CEILING_MB,
    AdminOverrides,
    AppConfig,
    admin_overrides,
    effective_config,
    load_config,
    restore_admin_defaults,
    save_admin_changes,
    set_admin_overrides,
)
from incident_suite.graph import (
    RunContext,
    build_graph,
    connect_services,
    resume_run,
    start_run,
)
from incident_suite.integrations.jira_client import JiraClient
from incident_suite.integrations.slack_client import SlackClient
from incident_suite.parsing.readers import ReadError, parse_upload
from incident_suite.parsing.redact import Redactor
from incident_suite.rag.retriever import RunbookRetriever
from incident_suite.rag.runbooks import Runbook, load_runbooks
from incident_suite.security import (
    SHARED_RUNS_PER_HOUR,
    VISITOR_LIVE_RUNS_PER_HOUR,
    VISITOR_TICKETS_PER_RUN,
    RateLimitError,
    RunLimiter,
    UploadError,
    check_upload,
    display_name,
    passcode_ok,
)
from incident_suite.state import (
    ApprovalStatus,
    Issue,
    RunResult,
    RunSettings,
    RunStep,
)
from incident_suite.telemetry import enable_tracing, merge_usage, new_run_id, trace_link

SAMPLES = Path(__file__).resolve().parent / "sample_logs"
ICON = Path(__file__).resolve().parent / "assets" / "triagegraph.svg"
CHAT_HEIGHT = 380
# Layout only: a health bar pinned in the header strip, a fixed footer and a sticky assistant
# column. Static, so it can go through st.markdown with unsafe_allow_html: no log or user text
# ever goes in.
STYLE = """
<style>
.st-key-topbar {
  position: fixed; top: 0.7rem; left: 50%; transform: translateX(-50%); z-index: 999991;
  width: auto; max-width: 70vw;
}
.st-key-topbar p { margin: 0; white-space: nowrap; }
.st-key-footer {
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 999992; background: #0e1117;
  border-top: 1px solid #262730; padding: 0 5rem; height: 2.2rem;
  display: flex; justify-content: center; overflow: hidden;
}
.st-key-footer > div { width: 100%; }
.st-key-footer [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
.st-key-footer p {
  font-size: 0.78rem; color: #898781; margin: 0; line-height: 1.2; text-align: right;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
div[data-testid="stColumn"]:has(.st-key-assistant) {
  position: sticky; top: 4rem; align-self: flex-start;
}
.block-container { padding-top: 3.5rem; padding-bottom: 4.5rem; }
header[data-testid="stHeader"] { background: #0e1117; }  /* solid, so the page scrolls under it */
.block-container h1 { padding-top: 0; }
[data-testid="stSidebarHeader"] {
  position: absolute; top: 0; right: 0; height: auto; padding: 1.1rem 1rem 0 0; z-index: 2;
}
[data-testid="stSidebarUserContent"] { padding-top: 1.1rem; }
[data-testid="stSidebarUserContent"] h2 { padding-top: 0; }
.st-key-brand { padding-right: 2.75rem; }  /* room for the collapse button */
.st-key-brand h2 { font-size: 1.35rem; white-space: nowrap; }
.st-key-menu button {
  justify-content: flex-start; padding: 0.4rem 0.9rem; min-height: 2.5rem; border: 0;
}
.st-key-menu button > div { justify-content: flex-start; }
/* Chat: yours on the right, the assistant's on the left. */
div[class*="st-key-msgyou_"], div[class*="st-key-msgbot_"] {
  gap: 0.15rem; padding: 0.5rem 0.75rem; max-width: 85%;
}
div[class*="st-key-msgyou_"] {
  margin-left: auto; background: rgba(249, 115, 22, 0.16); border-radius: 14px 14px 4px 14px;
}
div[class*="st-key-msgyou_"] [data-testid="stCaptionContainer"] { text-align: right; }
div[class*="st-key-msgbot_"] {
  margin-right: auto; background: #1c222c; border-radius: 14px 14px 14px 4px;
}
div[class*="st-key-msgyou_"] p, div[class*="st-key-msgbot_"] p { margin-bottom: 0; }
div[class*="st-key-msgyou_"] [data-testid="stMarkdownContainer"],
div[class*="st-key-msgbot_"] [data-testid="stMarkdownContainer"],
div[class*="st-key-msgyou_"] [data-testid="stCaptionContainer"],
div[class*="st-key-msgbot_"] [data-testid="stCaptionContainer"] { margin-bottom: 0; }
div[data-testid="stMetricValue"] { font-size: 1.6rem; }
/* Narrow screens: the assistant leaves its column and becomes a menu item of its own. */
.st-key-menu_assistant { display: none; }
@media (max-width: 1199px) {
  .st-key-menu_assistant { display: block; }
  div[data-testid="stColumn"]:has(.st-key-assistant) { display: none; }
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .st-key-assistant)
    > div[data-testid="stColumn"] { width: 100% !important; flex: 1 1 100% !important; }
}
/* The rest reacts to the room the page actually has (an open left bar takes some). */
.block-container { container-type: inline-size; container-name: page; }
/* The health bar fits in the header strip from 1300px; below that it joins the page. */
@media (max-width: 1299px) {
  .st-key-topbar {
    position: static; transform: none; max-width: 100%; width: 100%; margin: 0.6rem 0 0.25rem;
    z-index: auto;  /* a flex item keeps its z-index even when static: stay under the menu */
  }
  .st-key-topbar p { white-space: normal; line-height: 2; font-size: 0.85rem; }
}
@container page (max-width: 860px) {
  .st-key-footer {
    position: static; height: auto; padding: 1rem 0 0; border: 0; background: transparent;
    z-index: auto;  /* like the health bar: stay under the open menu */
  }
  .st-key-footer p { white-space: normal; text-align: center; }
  .block-container { padding-bottom: 2rem; }
  div[data-testid="stMetricValue"] { font-size: 1.2rem; }
}
</style>
"""
# Pasted text goes through the same checks, redaction and readers as an upload. The reader
# detects JSON or CSV from the content, so the .log name only decides the allowed type.
PASTED_NAME = "pasted.log"
PASTE_MAX_CHARS = 1_000_000
_LOG = logging.getLogger("triagegraph")
RUN_FAILED = (
    "Something went wrong during this run, so nothing was sent. If the app was updated a "
    "moment ago, reload the page and try again. Otherwise try another log or come back soon."
)
# Each sample is its own run, so a few at once stays inside the 5 runs per 10 minutes limit.
MAX_SAMPLES = 3
# What one run keeps in the session. Several runs are kept under "runs", and the one on screen
# is copied to the top level, so the rest of the page reads a single run as before.
RUN_KEYS = ("context", "run_id", "result", "run_note", "assistant", "chat")


# ------------------------------------------------------------------ cached resources


@st.cache_data
def _samples() -> dict[str, str]:
    """Sample file name to its one-line scenario, from ``labels.json``."""
    labels = json.loads((SAMPLES / "labels.json").read_text(encoding="utf-8"))
    return {log["file"]: log["scenario"] for log in labels["logs"]}


@st.cache_resource
def _shared_limiter() -> RunLimiter:
    """One limit shared by every visitor, so new browser sessions can't get around it."""
    return RunLimiter(max_runs=SHARED_RUNS_PER_HOUR, window=3600)


@st.cache_resource(show_spinner="Indexing the runbooks (once per model and key)...")
def _retriever(embedding_model: str, key_id: str) -> RunbookRetriever:  # noqa: ARG001 - cache key
    """The runbook index, shared by every session. Rebuilt if an admin changes model or key."""
    config = effective_config().model_copy(update={"embedding_model": embedding_model})
    return RunbookRetriever(load_runbooks(), make_embeddings(config))


def _session_retriever(config: AppConfig) -> RunbookRetriever:
    """The shared index, or, once runbook drafts are approved, this session's own index."""
    approved: dict[str, tuple[Runbook, str]] = st.session_state.get("approved_runbooks", {})
    if not approved:
        key = config.openrouter_api_key.get_secret_value() if config.openrouter_api_key else ""
        key_id = hashlib.sha256(key.encode()).hexdigest()[:12]  # never the key itself
        return _retriever(config.embedding_model, key_id)
    ids = tuple(sorted(approved))
    cached = st.session_state.get("draft_index")
    if cached is None or cached[0] != ids:
        drafts = [runbook for runbook, _ in approved.values()]
        index = RunbookRetriever([*load_runbooks(), *drafts], make_embeddings(config))
        st.session_state.draft_index = cached = (ids, index)
    retriever: RunbookRetriever = cached[1]
    return retriever


def _session(key: str, default: Any) -> Any:
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


# ------------------------------------------------------------------ sidebar and input


def _lock() -> None:
    st.session_state.admin_ok = False


def _stop_unlocking() -> None:
    st.session_state.pop("unlocking", None)


def _start_unlocking() -> None:
    st.session_state.unlocking = True


@st.dialog("Admin passcode", icon=":material/lock:", on_dismiss=_stop_unlocking)
def _unlock(passcode: SecretStr) -> None:
    """Asked once per session, from the Unlock button on a settings page."""
    st.caption("Settings change the app for every visitor, so they need the admin passcode.")
    with st.form("unlock_form", border=False):  # a form, so Enter submits
        entered = st.text_input("Passcode", type="password", key="passcode")
        submitted = st.form_submit_button(
            "Unlock", icon=":material/lock_open:", type="primary", key="unlock"
        )
    if submitted:
        if passcode_ok(entered, passcode):
            st.session_state.admin_ok = True
            _stop_unlocking()
            st.rerun()
        st.error("That passcode isn't right.")


def _lock_state(base: AppConfig) -> bool:
    """Whether a settings page is read-only, with Unlock or Lock to switch."""
    if base.admin_passcode is None:
        st.caption("Set ADMIN_PASSCODE in the deployment's settings to change anything here.")
        return True
    if st.session_state.get("admin_ok"):
        st.button("Lock", icon=":material/lock:", on_click=_lock, key="lock")
        return False
    st.caption("Read-only. Unlock with the admin passcode to change settings.")
    st.button("Unlock", icon=":material/lock_open:", on_click=_start_unlocking, key="unlock_page")
    if st.session_state.get("unlocking"):
        _unlock(base.admin_passcode)
    return True


def _save(card: str, changes: dict[str, Any]) -> None:
    """Save one card for everyone, or keep its problems to show under it.

    After a save the key boxes get fresh widgets, so a typed key doesn't linger in the session,
    and the page reruns so everything shows the new settings.
    """
    problems = save_admin_changes(changes)
    st.session_state.console_note = (card, problems)
    if not problems:
        st.session_state.secret_round = st.session_state.get("secret_round", 0) + 1
        st.session_state.saved_toast = "Saved for everyone, until the app sleeps or restarts."
        st.rerun()


def _restore(card: str, fields: list[str]) -> None:
    """Drop this card's changes, so its settings come from the deployment again."""
    restore_admin_defaults(fields)
    for key in [k for k in st.session_state if str(k).startswith(f"{card}_")]:
        del st.session_state[key]  # keyed pickers would otherwise keep the old choice
    st.session_state.console_note = None
    st.session_state.secret_round = st.session_state.get("secret_round", 0) + 1
    st.session_state.saved_toast = "Restored the defaults for everyone."


def _actions(card: str, changes: dict[str, Any], locked: bool) -> None:
    """Save and Restore defaults for one card, then any problems from the last save."""
    changed = admin_overrides().differences(load_config()).keys() & changes.keys()
    row = st.container(horizontal=True, gap="small")
    if row.button("Save", icon=":material/save:", key=f"save_{card}", disabled=locked):
        _save(card, changes)
    row.button(
        "Restore defaults",
        icon=":material/restart_alt:",
        key=f"restore_{card}",
        disabled=locked or not changed,
        on_click=_restore,
        args=(card, sorted(changes)),
        help="Drop this card's changes and use the deployment's own settings again.",
    )
    _note(card)


def _note(card: str) -> None:
    note = st.session_state.get("console_note")
    if note is not None and note[0] == card:
        for problem in note[1]:
            st.error(ui.md(problem))


def _field(label: str, value: str | None, locked: bool) -> str:
    """A setting that names a person or a site: masked for visitors, in full once unlocked."""
    if locked and value:
        st.text_input(f"{label} (masked)", ui.mask(value), disabled=True)
        return value
    return st.text_input(label, value or "", disabled=locked)


def _secret(label: str, current: SecretStr | None, key: str, locked: bool) -> SecretStr:
    """A key box that never sends the key to the browser, only a hint of it.

    Left blank, the current key stays.
    """
    hint = ui.secret_hint(current)
    typed = st.text_input(
        label,
        type="password",
        key=f"{key}_{st.session_state.get('secret_round', 0)}",
        disabled=locked,
        placeholder=f"{hint}; type to replace" if current else "not set",
        help="Keys are always masked: the full key never reaches the page.",
    )
    return SecretStr(typed)


def _model_picker(card: str, current: str, models: Iterable[str], locked: bool) -> str:
    """Provider, then model; the price shows under the model."""
    grouped = ui.models_by_provider(models)
    providers = list(grouped)
    provider = st.selectbox(
        "Provider",
        providers,
        index=providers.index(ui.provider_of(current)),
        key=f"{card}_provider",
        disabled=locked,
    )
    choices = grouped[provider]
    model = st.selectbox(
        "Model",
        choices,
        index=choices.index(current) if current in choices else 0,
        key=f"{card}_model",
        disabled=locked,
    )
    price = ui.embedding_label(model) if model in EMBEDDING_MODELS else ui.model_label(model)
    st.caption(ui.md(price.removeprefix(f"{model} ").strip("()")))  # md: "$" isn't maths
    return str(model)


def _agent_settings(config: AppConfig, locked: bool) -> None:
    with st.container(border=True):
        st.markdown("**:material/key: OpenRouter and budget**")
        key = _secret("OpenRouter API key", config.openrouter_api_key, "or_key", locked)
        budget = st.number_input(
            "Token budget per run",
            1_000,
            RUN_TOKEN_BUDGET_CEILING,
            config.run_token_budget,
            step=10_000,
            disabled=locked,
            help="A run stops calling the models once it has used this many tokens.",
        )
        _actions("models", {"openrouter_api_key": key, "run_token_budget": budget}, locked)
    with st.container(border=True):
        st.markdown("**:material/manage_search: Log Reader / Classifier**")
        model = _model_picker("log_reader", config.classifier_model, CHAT_MODELS, locked)
        lines = st.slider("Context lines", 0, 20, config.context_lines, disabled=locked)
        st.caption("Lines kept around each error, so the model sees what led up to it.")
        _actions("log_reader", {"classifier_model": model, "context_lines": lines}, locked)
    with st.container(border=True):
        st.markdown("**:material/build: Remediation**")
        model = _model_picker("remediation", config.remediation_model, CHAT_MODELS, locked)
        top_k = st.slider("Runbook matches (top-k)", 1, 10, config.runbook_top_k, disabled=locked)
        st.caption("How many of the best-matching runbooks the model reads for each issue.")
        threshold = st.slider(
            "Retry below this confidence", 0.0, 1.0, config.retry_confidence, 0.05, disabled=locked
        )
        st.caption("Below this, the issue goes back once with more of the log around it.")
        _actions(
            "remediation",
            {
                "remediation_model": model,
                "runbook_top_k": top_k,
                "retry_confidence": threshold,
            },
            locked,
        )
    with st.container(border=True):
        st.markdown("**:material/checklist: Cookbook**")
        current = config.cookbook_model or config.classifier_model
        model = _model_picker("cookbook", current, CHAT_MODELS, locked)
        _actions("cookbook", {"cookbook_model": model}, locked)
    with st.container(border=True):
        st.markdown("**:material/hub: Embedding (runbook search)**")
        model = _model_picker("embedding", config.embedding_model, EMBEDDING_MODELS, locked)
        _actions("embedding", {"embedding_model": model}, locked)


def _app_settings(config: AppConfig, locked: bool) -> None:
    with st.container(border=True):
        st.markdown("**:material/tune: General**")
        mode = st.segmented_control(
            "Run mode",
            ["Live", "Dry run"],
            default="Live" if config.live_integrations else "Dry run",
            disabled=locked,
            key="run_mode",
            help="Live: everyone's approved runs post to Slack and JIRA (visitors: sample logs "
            "only, 1 ticket per run). Dry run: nothing is sent, for anyone.",
        )
        live = mode != "Dry run"
        approval = st.toggle("Human approval", config.require_approval, disabled=locked)
        upload = st.slider(
            "Max upload size (MB)", 1, UPLOAD_CEILING_MB, config.max_upload_mb, disabled=locked
        )
        tickets = st.slider(
            "Max tickets per run",
            1,
            MAX_TICKETS_CEILING,
            config.max_tickets_per_run,
            disabled=locked,
        )
        _actions(
            "general",
            {
                "live_integrations": live,
                "require_approval": approval,
                "max_upload_mb": upload,
                "max_tickets_per_run": tickets,
            },
            locked,
        )
    with st.container(border=True):
        st.markdown("**:material/forum: Slack**")
        token = _secret("Bot token (xoxb-...)", config.slack_bot_token, "slack_token", locked)
        channel = _field("Channel ID", config.slack_channel_id, locked)
        slack = {"slack_bot_token": token, "slack_channel_id": channel.strip() or None}
        _actions("slack", slack, locked)
    with st.container(border=True):
        st.markdown("**:material/confirmation_number: JIRA**")
        url = _field("Base URL", config.jira_base_url, locked)
        email = _field("Email", config.jira_email, locked)
        project = st.text_input("Project key", config.jira_project_key or "", disabled=locked)
        token = _secret("API token", config.jira_api_token, "jira_token", locked)
        st.text_input("Issue type", config.jira_issue_type, disabled=True, help="Fixed for now.")
        _actions(
            "jira",
            {
                "jira_base_url": url.strip().rstrip("/") or None,
                "jira_email": email.strip() or None,
                "jira_project_key": project.strip().upper() or None,
                "jira_api_token": token,
            },
            locked,
        )
    with st.container(border=True):
        st.markdown("**:material/timeline: LangSmith**")
        tracing = st.toggle("Tracing", config.langsmith_tracing, disabled=locked)
        project = st.text_input("Project", config.langsmith_project, disabled=locked)
        key = _secret("API key", config.langsmith_api_key, "ls_key", locked)
        _actions(
            "langsmith",
            {
                "langsmith_tracing": tracing,
                "langsmith_project": project.strip() or None,
                "langsmith_api_key": key,
            },
            locked,
        )


def _back_to_defaults() -> None:
    set_admin_overrides(AdminOverrides())
    st.session_state.console_note = None


PAGES = {
    "analyze": ("Analyze", ":material/monitoring:"),
    "agents": ("Agent Settings", ":material/smart_toy:"),
    "app": ("App Settings", ":material/settings:"),
    "assistant": ("Assistant", ":material/forum:"),  # in the menu only on narrow screens
}
SETTINGS_PAGES = ("agents", "app")
UNLOCKED_MARK = ":material/lock_open:"  # on the settings items while the passcode is in


def _menu() -> str:
    """Left bar: the app's name and the menu, nothing else. Returns the chosen page."""
    current = str(st.session_state.get("page", "analyze"))
    with st.sidebar:
        with st.container(horizontal=True, vertical_alignment="center", gap="small", key="brand"):
            st.image(str(ICON), width=34)
            st.markdown("## TriageGraph")
        st.caption("Multi-agent incident triage")
        unlocked = bool(st.session_state.get("admin_ok"))
        with st.container(key="menu"):
            chosen = [
                page
                for page, (label, icon) in PAGES.items()
                if st.button(
                    f"{label} {UNLOCKED_MARK}" if unlocked and page in SETTINGS_PAGES else label,
                    icon=icon,
                    key=f"menu_{page}",
                    type="primary" if page == current else "tertiary",
                    width="stretch",
                    help="Unlocked for this session. Press Lock on the page to lock it again."
                    if unlocked and page in SETTINGS_PAGES
                    else None,
                )
            ]
    if chosen and chosen[0] != current:
        _stop_unlocking()
        st.session_state.page = chosen[0]
        st.rerun()
    return current


def _settings_page(config: AppConfig, page: str) -> None:
    """Agent or App Settings: for every visitor, behind the admin passcode (plan section 17.1)."""
    base = load_config()
    label, icon = PAGES[page]
    st.title(f"{icon} {label}")
    st.caption("A saved change applies to every visitor until the app sleeps or restarts.")
    toast = st.session_state.pop("saved_toast", None)
    if toast:
        st.toast(str(toast), icon=":material/check:")
    locked = _lock_state(base)
    if page == "agents":
        _agent_settings(config, locked)
    else:
        _app_settings(config, locked)
    changed = admin_overrides().differences(base)
    if changed:
        st.caption(
            "Changed from the defaults until the app restarts: " + ", ".join(sorted(changed))
        )
        st.button(
            "Back to defaults",
            icon=":material/restart_alt:",
            on_click=_back_to_defaults,
            disabled=locked,
            key="back_to_defaults",
        )


def _live_allowed(config: AppConfig) -> bool:
    """Run mode: Live for everyone, or Dry run for everyone. Visitors get guards in _start."""
    return config.live_integrations


def _unlocked() -> bool:
    return bool(st.session_state.get("admin_ok"))


def _is_sample(data: bytes, filename: str) -> bool:
    """True only for one of our own sample logs, byte for byte."""
    path = SAMPLES / filename
    return filename in _samples() and path.is_file() and path.read_bytes() == data


@st.cache_resource
def _visitor_live_limiter() -> RunLimiter:
    """Live runs by visitors across every session: they post to real Slack and JIRA."""
    return RunLimiter(max_runs=VISITOR_LIVE_RUNS_PER_HOUR, window=3600)


def _visitor_guard(live: bool, data: bytes, filename: str) -> tuple[bool, str]:
    """Visitors run live only with the sample logs, within their own hourly limit."""
    if not live or _unlocked():
        return live, ""
    if not _is_sample(data, filename):
        return False, "Uploads and pasted logs run as a dry run for visitors. Samples run live."
    try:
        _visitor_live_limiter().check()
    except RateLimitError:
        return False, "The hourly limit for live runs by visitors is reached, so this run is dry."
    return True, ""


def _top_bar(config: AppConfig, live: bool) -> None:
    """Sticky health bar: what's connected (never the keys) and whether runs send for real."""
    with st.container(key="topbar"):
        st.markdown(ui.health_markdown(config, live))


def _footer(config: AppConfig) -> None:
    """Fixed footer with the version and the limits that apply to every run."""
    with st.container(key="footer"):
        st.markdown(ui.footer_text(config, __version__))


def _pick_logs() -> list[tuple[bytes, str]]:
    """The chosen samples, upload or pasted text, each as bytes and a file name."""
    source = st.radio("Log", ["Sample logs", "Upload your own", "Paste text"], horizontal=True)
    if source == "Sample logs":
        samples = _samples()
        names = st.multiselect(
            f"Samples (up to {MAX_SAMPLES}; each one is its own run)",
            list(samples),
            default=list(samples)[:1],
            max_selections=MAX_SAMPLES,
        )
        for name in names:
            st.caption(ui.md(f"{name}: {samples[name]}"))
        return [((SAMPLES / name).read_bytes(), name) for name in names]
    if source == "Paste text":
        text = st.text_area(
            "Paste log lines (JSON lines and CSV work too)",
            height=220,
            max_chars=PASTE_MAX_CHARS,
            placeholder="2026-10-11T03:12:09Z ERROR payment-api Connection refused to db:5432",
        )
        return [(text.encode("utf-8"), PASTED_NAME)] if text.strip() else []
    upload = st.file_uploader(
        "Ops log: text, JSON lines, JSON, CSV, TSV or Parquet, optionally gzipped",
    )
    return [(upload.getvalue(), upload.name)] if upload is not None else []


# ------------------------------------------------------------------ running


def _progress(steps: list[RunStep], chart: Any, log: Any) -> Any:
    """A callback that lights up the graph and lists each step as it finishes."""

    def on_step(step: RunStep) -> None:
        steps.append(step)
        chart.graphviz_chart(ui.pipeline_dot(steps, running=True))
        log.write(f"{ui.LABELS.get(step.node, step.node)}: {ui.md(step.detail)}")

    return on_step


def _failed(filename: str, message: str) -> None:
    """Show why a log didn't run, and keep it to show again above the other runs' results."""
    st.error(message)
    _session("run_errors", []).append(f"{ui.md(display_name(filename))}: {message}")


def _start(config: AppConfig, live: bool, data: bytes, filename: str) -> None:
    """Check, parse and run one log, then keep everything the results screen needs."""
    try:
        check_upload(filename, len(data), config.max_upload_mb)
        _session("limiter", RunLimiter()).check()
        _shared_limiter().check()
    except (RateLimitError, UploadError) as exc:
        _failed(filename, ui.md(str(exc)))
        return
    if not config.llm_configured:
        _failed(filename, "This app needs OPENROUTER_API_KEY to analyse logs. See the README.")
        return
    live, note = _visitor_guard(live, data, filename)
    settings = RunSettings.from_config(config).model_copy(update={"live": live})
    if live and not _unlocked():
        cap = min(settings.jira.max_tickets, VISITOR_TICKETS_PER_RUN)
        settings = settings.model_copy(
            update={"jira": settings.jira.model_copy(update={"max_tickets": cap})}
        )
    try:
        parsed = parse_upload(data, display_name(filename), settings.redaction)
        retriever = _session_retriever(config)
    except ReadError as exc:
        _failed(filename, ui.md(str(exc)))
        return
    except openai.OpenAIError as exc:
        _failed(
            filename, f"Couldn't index the runbooks ({type(exc).__name__}). Try again in a minute."
        )
        return
    tracker, messenger = connect_services(config, settings)
    trace_id = uuid4() if enable_tracing(config) else None
    context = RunContext(
        parsed=parsed,
        settings=settings,
        llm=OpenRouterLLM(config),
        retriever=retriever,
        tracker=tracker,
        messenger=messenger,
        trace_id=trace_id,
        trace_url=trace_link(config, trace_id) if trace_id else None,
    )
    graph = _session("graph", build_graph())
    run_id = new_run_id()
    steps: list[RunStep] = []
    with st.status(f"Analysing {ui.md(parsed.name)}...", expanded=True) as status:
        chart = st.empty()
        chart.graphviz_chart(ui.pipeline_dot(steps, running=True))
        try:
            result = start_run(graph, context, run_id, _progress(steps, chart, st.container()))
        except Exception:  # show a clear message, never a raw traceback
            _LOG.exception("run %s failed", run_id)
            status.update(label=f"Couldn't analyse {ui.md(parsed.name)}", state="error")
            _failed(filename, RUN_FAILED)
            return
        status.update(label=f"Finished {ui.md(parsed.name)}", state="complete", expanded=False)
    _stash()
    st.session_state.update(
        context=_slimmed(context, result),
        run_id=run_id,
        result=result,
        run_note=note,
        assistant=None,
        chat=[],
    )
    st.session_state.run_name = parsed.name
    _stash()


def _stash() -> None:
    """Keep the run on screen under "runs", with any approval, chat or assistant answer."""
    state = st.session_state
    if "run_name" in state and "result" in state:
        _session("runs", {})[state.run_name] = {key: state.get(key) for key in RUN_KEYS}


def _switch() -> None:
    """Show another of this session's runs."""
    _stash()
    name = st.session_state.run_pick
    st.session_state.update(st.session_state.runs[name], run_name=name)


def _run_picker() -> None:
    """With several runs, a switch between them above the results."""
    runs: dict[str, Any] = st.session_state.get("runs", {})
    if len(runs) > 1:
        st.session_state.run_pick = st.session_state.run_name
        st.segmented_control("Runs", list(runs), key="run_pick", on_change=_switch, required=True)


def _answer(approved: bool) -> None:
    """Resume the paused run with the person's decision."""
    state = st.session_state
    with st.spinner("Sending..." if approved else "Recording the rejection..."):
        try:
            state.result = resume_run(state.graph, state.context, state.run_id, approved)
        except Exception:  # show a clear message, never a raw traceback
            _LOG.exception("resuming run %s failed", state.run_id)
            state.run_errors = [RUN_FAILED]
            return
    state.context = _slimmed(state.context, state.result)


def _slimmed(context: RunContext, result: RunResult) -> RunContext:
    """Once a run is done, keep only the lines the results screen needs (memory: plan 11).

    A paused run keeps the whole log until it is approved or rejected.
    """
    if result.approval is ApprovalStatus.PENDING:
        return context
    lines = ui.slim_lines(context.parsed.lines)
    if len(lines) == len(context.parsed.lines):
        return context
    slim = replace(context, parsed=context.parsed.model_copy(update={"lines": lines}))
    gc.collect()
    return slim


# ------------------------------------------------------------------ results


def _summary(result: RunResult) -> None:
    top = result.top_severity
    tokens = sum(u.input_tokens + u.output_tokens for u in result.usage)
    cols = st.columns(5)
    cols[0].metric("Worst severity", top.upper() if top else "none")
    cols[1].metric("Issues", len(result.issues))
    cols[2].metric("Lines to patterns", f"{result.line_count:,} to {len(result.patterns)}")
    cols[3].metric("Tokens", f"{tokens:,}")
    cols[4].metric("Cost", ui.money(result.total_cost_usd))
    st.caption(
        f"Run {result.run_id} | log {ui.md(result.log_name)} | fingerprint "
        f"{result.log_fingerprint} | {'dry run' if result.dry_run else 'live'}"
    )
    if result.issues:
        st.download_button(
            "Download postmortem draft (.md)",
            ui.postmortem_markdown(result, st.session_state.context.parsed.lines),
            file_name=f"postmortem-{result.run_id}.md",
            mime="text/markdown",
        )
    st.markdown(ui.legend_markdown())
    for error in result.errors:
        st.warning(ui.md(error))


def _approval(result: RunResult) -> None:
    with st.container(border=True):
        st.subheader("Approval needed")
        st.write("Nothing has been sent yet. This is exactly what will go to Slack and JIRA.")
        _outbox(result)
        row = st.container(horizontal=True, gap="small")  # buttons as wide as their labels
        row.button("Approve and send", type="primary", on_click=_answer, args=(True,))
        row.button("Reject", on_click=_answer, args=(False,))


def _outbox(result: RunResult) -> None:
    """The Slack and JIRA previews, or what was actually sent."""
    issues = {issue.id: issue for issue in result.issues}
    st.markdown("**JIRA**")
    if not result.tickets:
        st.write("No ticket: nothing reached the ticket severity.")
    for ticket in result.tickets:
        st.text(ui.ticket_preview(ticket, issues[ticket.issue_id], result.run_id))
        if ticket.sent and ticket.url and ticket.key:
            st.link_button(f"Open {ticket.key}", ticket.url)
    st.markdown("**Slack**")
    plan = result.notification
    if plan is None or not plan.post:
        st.write(plan.reason if plan else "Nothing to post.")
        return
    st.code(plan.summary, language=None, wrap_lines=True)
    with st.expander("Thread reply"):
        st.code(plan.thread, language=None, wrap_lines=True)
    if result.slack.permalink:
        st.link_button("Open the Slack message", result.slack.permalink)


def _card(issue: Issue, result: RunResult, evidence: dict[int, str]) -> None:
    fixes = {fix.issue_id: fix for fix in result.remediations}
    tickets = {ticket.issue_id: ticket for ticket in result.tickets}
    fix = fixes.get(issue.id)
    structured = result.log_format not in ui.TEXT_FORMATS
    with st.container(border=True):
        st.markdown(f"{ui.severity_badge(issue.severity)} **{ui.md(issue.title)}**")
        st.caption(
            f"{issue.id} | {issue.category} | {ui.md(issue.affected_service or 'unknown service')}"
            f" | {ui.evidence_label(issue.evidence_lines, structured)}"
        )
        left, right = st.columns([3, 2])
        with left:
            if fix is None:
                st.write("No fix was suggested for this issue.")
            else:
                st.markdown(f"**Likely root cause (a hypothesis):** {ui.md(fix.root_cause)}")
                steps = "\n".join(f"{n}. {ui.md(s)}" for n, s in enumerate(fix.steps, start=1))
                st.markdown(f"**Recommended fix**\n\n{steps}")
                st.caption(f"Why: {ui.md(fix.rationale)}")
        with right:
            score = ui.confidence(issue, fix)
            st.progress(score, text=f"Confidence {score:.0%}")
            reasons = ui.confidence_reasons(issue, fix, tickets.get(issue.id), result.dry_run)
            st.markdown("\n".join(f"- {ui.md(reason)}" for reason in reasons))
        with st.expander("Evidence"):
            lines = [f"{n:>7}  {evidence[n]}" for n in issue.evidence_lines if n in evidence]
            st.code("\n".join(lines) or "Not available", language=None, wrap_lines=True)
        ticket = tickets.get(issue.id)
        if ticket is not None and ticket.previous_fix:
            st.info(
                f"**Seen before in {ticket.related_key}.** Last time it was fixed like this: "
                f"{ui.md(ticket.previous_fix)}"
            )
        if ui.needs_runbook(fix):
            _draft_box(f"{result.run_id}-{issue.id}", issue.id)


# ------------------------------------------------------------------ runbook drafts


def _make_draft(key: str, issue_id: str) -> None:
    """Ask the assistant for a runbook, within the run's token budget, and count its cost."""
    state = st.session_state
    result: RunResult = state.result
    context: RunContext = state.context
    spent = sum(u.input_tokens + u.output_tokens for u in result.usage)
    if spent >= context.settings.token_budget:
        state.drafts[key] = DraftResult(error="This run has used its token budget.")
        return
    issue = next(i for i in result.issues if i.id == issue_id)
    fix = next((f for f in result.remediations if f.issue_id == issue_id), None)
    by_number = {line.line_no: line.raw for line in context.parsed.lines}
    evidence = [by_number[n] for n in issue.evidence_lines if n in by_number]
    draft = draft_runbook(
        issue,
        evidence,
        fix,
        OpenRouterLLM(effective_config()),
        Redactor(context.settings.redaction),
    )
    state.drafts[key] = draft
    if draft.usage is not None:
        state.result = result.model_copy(
            update={"usage": merge_usage([*result.usage, draft.usage])}
        )


def _approve_draft(key: str) -> None:
    draft: DraftResult = st.session_state.drafts[key]
    if draft.runbook is not None and not draft.problems:
        st.session_state.approved_runbooks[draft.runbook.id] = (draft.runbook, draft.text)


def _discard_draft(key: str) -> None:
    st.session_state.drafts.pop(key, None)


def _draft_box(key: str, issue_id: str) -> None:
    """The offer to draft a runbook, then the draft with Approve and Discard (plan 17.3)."""
    drafts: dict[str, DraftResult] = _session("drafts", {})
    approved: dict[str, tuple[Runbook, str]] = _session("approved_runbooks", {})
    draft = drafts.get(key)
    if draft is None:
        st.caption("No runbook matched this issue well.")
        if st.button("Draft a runbook (AI)", key=f"draft-{key}"):
            with st.spinner("Drafting a runbook from the evidence..."):
                _make_draft(key, issue_id)
            st.rerun()
        return
    if draft.error:
        st.warning(ui.md(draft.error))
        st.button("Try again", key=f"retry-{key}", on_click=_discard_draft, args=(key,))
        return
    if draft.runbook is None:
        return
    st.warning(f"{DRAFT_LABEL}. Check every step before you rely on it.")
    st.code(draft.text, language="markdown", wrap_lines=True)
    if draft.runbook.id in approved:
        st.success("Approved: later runs in this session can use it. It isn't saved anywhere else.")
        st.download_button(
            f"Download {draft.runbook.id}.md",
            draft.text,
            file_name=f"{draft.runbook.id}.md",
            mime="text/markdown",
            key=f"download-{key}",
        )
        return
    for problem in draft.problems:
        st.error(f"Can't approve: {ui.md(problem)}")
    row = st.container(horizontal=True, gap="small")
    row.button(
        "Approve",
        key=f"approve-{key}",
        on_click=_approve_draft,
        args=(key,),
        disabled=bool(draft.problems),
    )
    row.button("Discard", key=f"discard-{key}", on_click=_discard_draft, args=(key,))


def _tabs(result: RunResult, context: RunContext) -> None:
    parsed = context.parsed
    tabs = st.tabs(
        [
            "Checklist",
            "Slack and JIRA",
            "Timeline",
            "Parsed log",
            "Patterns",
            "Runbooks",
            "Run log",
            "Replay",
            "Cost",
        ]
    )
    with tabs[0]:
        for heading, items in (
            ("Do now", result.checklist.do_now),
            ("This week", result.checklist.this_week),
            ("Prevent next time", result.checklist.prevent),
        ):
            st.markdown(f"**{heading}**")
            for n, item in enumerate(items):
                label = ui.md(f"{item.text} ({', '.join(item.issue_ids)})")
                st.checkbox(label, key=f"{result.run_id}-{heading}-{n}")
            if not items:
                st.caption("Nothing here.")
    with tabs[1]:
        if result.dry_run:
            st.info("Dry run: nothing was sent. This is exactly what would go out.")
        _outbox(result)
    with tabs[2]:
        timeline = ui.timeline_spec(parsed.lines)
        if timeline is None:
            st.caption("Not enough timestamped errors to draw a timeline.")
        else:
            spec, bucket = timeline
            st.caption(
                f"ERROR and FATAL lines per {bucket} minute(s), UTC. Dashed lines are deploys "
                "and config changes. Hover for details."
            )
            st.vega_lite_chart(spec)
    with tabs[3]:
        st.caption(ui.md(parsed.note))
        if parsed.redactions:
            counts = ", ".join(f"{kind}: {n}" for kind, n in parsed.redactions.items())
            st.write(f"Redacted before any agent saw the log: {counts}")
            st.caption(
                "; ".join(f"{kind}: {ui.REDACTION_LABELS[kind]}" for kind in parsed.redactions)
            )
        if parsed.column_mapping:
            st.write("Column mapping", parsed.column_mapping)
        if result.line_count > ui.MAX_TABLE_ROWS:
            st.caption(f"Showing the first {ui.MAX_TABLE_ROWS:,} of {result.line_count:,}.")
        st.dataframe(ui.line_rows(parsed.lines), hide_index=True)
    with tabs[4]:
        st.dataframe(ui.pattern_rows(result.patterns), hide_index=True)
    with tabs[5]:
        st.dataframe(ui.source_rows(result.remediations), hide_index=True)
    with tabs[6]:
        st.dataframe(ui.pipeline_strip(result.steps), hide_index=True)
        st.dataframe(ui.run_log_rows(result.steps), hide_index=True)
    with tabs[7]:
        _replay(result)
    with tabs[8]:
        st.dataframe(ui.usage_rows(result.usage), hide_index=True)
        if result.trace_url:
            st.link_button("Open the trace", result.trace_url)


def _replay(result: RunResult) -> None:
    """Step through a finished run: the graph lights up one step at a time (plan 17.6)."""
    if len(result.steps) < 2:
        st.caption("Nothing to replay yet.")
        return
    position = st.slider("Step", 1, len(result.steps), 1, key=f"replay-{result.run_id}")
    dot, caption = ui.replay_frame(result.steps, position)
    st.graphviz_chart(dot)
    st.caption(ui.md(caption))


def _results(result: RunResult, context: RunContext) -> None:
    _summary(result)
    if note := st.session_state.get("run_note"):
        st.info(note)
    st.graphviz_chart(ui.pipeline_dot(result.steps))
    if result.approval is ApprovalStatus.PENDING:
        _approval(result)
    elif result.approval is ApprovalStatus.REJECTED:
        st.info("Rejected: nothing was sent to Slack or JIRA.")
    elif result.approval is ApprovalStatus.APPROVED:
        st.success("Approved and sent. See the Slack and JIRA tab for links.")
    if not result.issues:
        st.success("No issues found. Nothing needs sending.")
    evidence = {line.line_no: line.raw for line in context.parsed.lines}
    worst_first = sorted(result.issues, key=lambda issue: -issue.severity.rank)
    for issue in worst_first:
        _card(issue, result, evidence)
    _tabs(result, context)


# ------------------------------------------------------------------ assistant


def _ask_assistant(config: AppConfig, progress: Callable[[str], None]) -> None:
    """Read this run's Slack threads and tickets, and keep the answer and its cost."""
    state = st.session_state
    result: RunResult = state.result
    settings: RunSettings = state.context.settings
    tracker, messenger = connect_services(config, settings.model_copy(update={"live": True}))
    try:
        answer = latest_updates(
            AssistantInput(
                result=result,
                threads=messenger,
                tickets=tracker,
                llm=OpenRouterLLM(config),
                redactor=Redactor(settings.redaction),
                token_budget=settings.token_budget,
                progress=progress,
            )
        )
    finally:
        for client in (tracker, messenger):
            if client is not None:
                client.close()
    state.assistant = answer
    if answer.usage is not None:
        state.result = result.model_copy(
            update={"usage": merge_usage([*result.usage, answer.usage])}
        )


def _ask_about_run(question: str) -> None:
    """Answer a question about the current run and add the answer to the chat."""
    state = st.session_state
    result: RunResult = state.result
    context: RunContext = state.context
    reply = ask_about_run(
        question,
        ui.chat_pairs(state.chat[:-1]),  # the question itself is already the last message
        result,
        {line.line_no: line.raw for line in context.parsed.lines},
        OpenRouterLLM(effective_config()),
        context.settings.token_budget,
    )
    state.chat.append(ui.ChatMessage("assistant", reply.answer or "", datetime.now(UTC)))
    if reply.error:
        state.chat_error = reply.error
    if reply.usage is not None:
        state.result = result.model_copy(
            update={"usage": merge_usage([*result.usage, reply.usage])}
        )


def _bubble(message: ui.ChatMessage, index: int, zone: str | None, offset: int | None) -> None:
    """One message: yours on the right, the assistant's on the left, each with name and time."""
    side = "msgyou" if message.role == "you" else "msgbot"
    with st.container(key=f"{side}_{index}"):
        st.caption(ui.md(ui.chat_label(message, zone, offset)))
        st.markdown(ui.md(message.text) if message.text else "_No answer._")


def _chat() -> None:
    """Chat about this run only (plan 17.4). Your question shows at once; the answer follows."""
    state = st.session_state
    chat: list[ui.ChatMessage] = _session("chat", [])
    pending: str | None = state.get("chat_pending")
    zone, offset = st.context.timezone, st.context.timezone_offset
    with st.container(height=CHAT_HEIGHT, border=False):
        if not chat:
            st.caption("Ask anything about this run: an issue, its evidence, the fix, the cost.")
        for index, message in enumerate(chat):
            _bubble(message, index, zone, offset)
        if pending:
            with st.container(key=f"msgbot_{len(chat)}"):
                st.caption("TriageGraph is thinking…")
    if error := state.pop("chat_error", None):
        st.warning(ui.md(error))
    question = st.chat_input(
        "Ask about this run",
        key="chat_input",
        max_chars=MAX_QUESTION_CHARS,
        disabled=bool(pending),
    )
    if question and question.strip():
        chat.append(ui.ChatMessage("you", question.strip(), datetime.now(UTC)))
        state.chat_pending = question.strip()
        st.rerun()  # show the question straight away, then answer it
    if pending:
        _ask_about_run(pending)
        state.chat_pending = None
        st.rerun()


def _assistant(config: AppConfig) -> None:
    """Right-hand column: a chat about this run (17.4), and Slack and JIRA updates (17.2)."""
    st.subheader(":material/forum: Assistant")
    if "result" not in st.session_state:
        st.caption("Run an analysis, then ask about it here.")
        with st.container(border=True):
            _earlier_incidents(config)
        return
    result: RunResult = st.session_state.result
    with st.container(border=True):
        _chat()
    with st.container(border=True):
        st.markdown("**:material/update: Since the run**")
        if not (result.slack.sent or any(t.sent for t in result.tickets)):
            st.caption("After a live run posts to Slack or JIRA, ask what has happened since.")
            return
        if st.button("What's happened since?") and _within_limits():
            with st.status("Checking Slack and JIRA...", expanded=True) as status:
                _ask_assistant(config, lambda message: status.write(ui.md(message)))
                status.update(label="Checked Slack and JIRA", state="complete", expanded=False)
        answer: AssistantResult | None = st.session_state.get("assistant")
        if answer is not None:
            _show_answer(answer)


def _show_answer(answer: AssistantResult) -> None:
    """The assistant's summary of Slack and JIRA, and what it read."""
    if answer.error:
        st.warning(ui.md(answer.error))
    if answer.answer is not None:
        st.markdown(ui.md(answer.answer.summary))
        st.markdown("\n".join(f"- {ui.md(line)}" for line in answer.answer.updates))
        if answer.answer.next_step:
            st.markdown(f"**Next:** {ui.md(answer.answer.next_step)}")
    if answer.sources:
        st.caption("Read: " + ui.md("; ".join(answer.sources)))


def _readers(config: AppConfig) -> tuple[JiraClient | None, SlackClient | None]:
    """Read-only JIRA and Slack clients for the assistant, whatever the run's mode."""
    settings = RunSettings.from_config(config).model_copy(update={"live": True})
    return connect_services(config, settings)


def _load_incidents(config: AppConfig) -> None:
    tracker, messenger = _readers(config)
    try:
        st.session_state.incidents = tracker.recent() if tracker is not None else []
    except ReaderError as exc:
        st.session_state.incidents = str(exc)
    finally:
        for client in (tracker, messenger):
            if client is not None:
                client.close()


def _ask_about_incident(config: AppConfig, key: str, progress: Callable[[str], None]) -> None:
    """What has happened to one earlier incident: its ticket and its Slack thread."""
    tracker, messenger = _readers(config)
    try:
        answer = latest_updates(
            AssistantInput(
                result=None,
                keys=(key,),
                threads=messenger,
                tickets=tracker,
                llm=OpenRouterLLM(config),
                redactor=Redactor(),
                token_budget=config.run_token_budget,
                progress=progress,
            )
        )
    finally:
        for client in (tracker, messenger):
            if client is not None:
                client.close()
    st.session_state.incident_answer = (key, answer)


def _within_limits() -> bool:
    """Assistant questions spend the model budget, so they count towards the run limits too."""
    try:
        _session("limiter", RunLimiter()).check()
        _shared_limiter().check()
    except RateLimitError as exc:
        st.warning(ui.md(str(exc)))
        return False
    return True


def _earlier_incidents(config: AppConfig) -> None:
    """Without a run: pick one of our latest JIRA tickets and ask what has happened since."""
    st.markdown("**:material/history: Earlier incidents**")
    if not config.jira_configured:
        st.caption("Connect JIRA to ask about earlier incidents.")
        return
    if st.button("Load the latest incidents", icon=":material/refresh:", key="load_incidents"):
        with st.spinner("Reading JIRA..."):
            _load_incidents(config)
    incidents: list[RecentTicket] | str | None = st.session_state.get("incidents")
    if incidents is None:
        return
    if isinstance(incidents, str):
        st.warning(ui.md(incidents))
        return
    if not incidents:
        st.caption("No TriageGraph tickets yet.")
        return
    labels = {t.key: f"{t.key} · {t.status} · {t.created} · {t.summary}" for t in incidents}
    key = st.selectbox("Incident", list(labels), format_func=labels.__getitem__)
    if st.button("What's happened since?", key="ask_incident") and _within_limits():
        with st.status(f"Checking {key}...", expanded=True) as status:
            _ask_about_incident(config, key, lambda message: status.write(ui.md(message)))
            status.update(label=f"Checked {key}", state="complete", expanded=False)
    asked = st.session_state.get("incident_answer")
    if asked is not None and asked[0] == key:
        _show_answer(asked[1])
        if asked[1].usage is not None:
            st.caption(ui.md(f"Cost: ${asked[1].usage.cost_usd:.4f}"))


# ------------------------------------------------------------------ page


def _analyze(config: AppConfig, live: bool) -> None:
    """The middle of the Analyze page: pick a log, run it, show the results."""
    st.title("TriageGraph")
    st.caption(
        "Upload an ops log. Agents find the problems, suggest fixes with sources, write a "
        "checklist, and prepare the Slack post and JIRA tickets for your approval."
    )
    if live and not _unlocked():
        st.caption(
            "Live: approved runs of the sample logs post to Slack and JIRA (1 ticket per run). "
            "Uploads and pasted logs run dry."
        )
    picked = _pick_logs()
    clicked = st.button("Analyse", type="primary", disabled=not picked)
    area = st.empty()  # one slot: a new run replaces the old results straight away
    if clicked and picked:
        for key in (*RUN_KEYS, "runs", "run_name", "run_errors", "chat_pending"):
            st.session_state.pop(key, None)
        area.empty()  # drop the old results at once, not when the new run finishes
        with area.container():
            for data, filename in picked:
                _start(config, live, data, filename)
        if "result" in st.session_state:
            st.rerun()
    elif "result" in st.session_state:
        with area.container():
            for error in st.session_state.get("run_errors", []):
                st.error(error)
            _run_picker()
            _results(st.session_state.result, st.session_state.context)


def main() -> None:
    """Render the page."""
    st.set_page_config(page_title="TriageGraph", page_icon=str(ICON), layout="wide")
    config = effective_config()
    st.markdown(STYLE, unsafe_allow_html=True)  # static CSS only, never user data
    page = _menu()
    live = _live_allowed(config)
    _top_bar(config, live)
    if page == "assistant":  # narrow screens: the assistant gets the whole middle
        _assistant(config)
    else:
        main_column, assistant_column = st.columns([3, 1.25], gap="large")
        with main_column:
            if page == "analyze":
                _analyze(config, live)
            else:
                _settings_page(config, page)
        with assistant_column, st.container(key="assistant"):
            _assistant(config)
    _footer(config)


main()
