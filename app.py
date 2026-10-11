"""Streamlit entry point. UI only: all logic lives in the ``incident_suite`` package.

The compiled graph and the run's context are kept in ``st.session_state``, so a paused run
survives the reruns Streamlit does on every click, and Approve resumes the same run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import openai
import streamlit as st

from incident_suite import __version__, ui
from incident_suite.agents import OpenRouterLLM, make_embeddings
from incident_suite.agents.assistant import (
    DRAFT_LABEL,
    MAX_QUESTION_CHARS,
    AssistantInput,
    AssistantResult,
    DraftResult,
    ask_about_run,
    draft_runbook,
    latest_updates,
)
from incident_suite.config import AppConfig, admin_overrides, effective_config, load_config
from incident_suite.graph import (
    RunContext,
    build_graph,
    connect_services,
    resume_run,
    start_run,
)
from incident_suite.parsing.readers import ReadError, parse_upload
from incident_suite.parsing.redact import Redactor
from incident_suite.rag.retriever import RunbookRetriever
from incident_suite.rag.runbooks import Runbook, load_runbooks
from incident_suite.security import (
    SHARED_RUNS_PER_HOUR,
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
# Pasted text goes through the same checks, redaction and readers as an upload. The reader
# detects JSON or CSV from the content, so the .log name only decides the allowed type.
PASTED_NAME = "pasted.log"
PASTE_MAX_CHARS = 1_000_000


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


@st.cache_resource(show_spinner="Indexing the runbooks (once per app start)...")
def _retriever(embedding_model: str) -> RunbookRetriever:
    """The runbook index, built once and shared by every session."""
    config = load_config().model_copy(update={"embedding_model": embedding_model})
    return RunbookRetriever(load_runbooks(), make_embeddings(config))


def _session_retriever(config: AppConfig) -> RunbookRetriever:
    """The shared index, or, once runbook drafts are approved, this session's own index."""
    approved: dict[str, tuple[Runbook, str]] = st.session_state.get("approved_runbooks", {})
    if not approved:
        return _retriever(config.embedding_model)
    ids = tuple(sorted(approved))
    cached = st.session_state.get("draft_index")
    if cached is None or cached[0] != ids:
        drafts = [runbook for runbook, _ in approved.values()]
        index = RunbookRetriever([*load_runbooks(), *drafts], make_embeddings(config))
        st.session_state.draft_index = cached = (ids, index)
    retriever: RunbookRetriever = cached[1]
    return retriever


def _settings(config: AppConfig) -> RunSettings:
    """This session's settings from the Agent settings page, or the deployment's defaults."""
    chosen = st.session_state.get("run_settings")
    return chosen if isinstance(chosen, RunSettings) else RunSettings.from_config(config)


def _session(key: str, default: Any) -> Any:
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


# ------------------------------------------------------------------ sidebar and input


def _lock() -> None:
    st.session_state.admin_ok = False


def _live_allowed(config: AppConfig) -> bool:
    """Live sending, if it's on, needs the admin passcode when one is set.

    Once entered, the passcode stays unlocked for the session (``admin_ok``), across pages.
    """
    if not config.live_integrations:
        return False
    if config.admin_passcode is None:
        return True
    if st.session_state.get("admin_ok"):
        st.button("Lock live sending", on_click=_lock)
        return True
    entered = st.text_input("Admin passcode for live sending", type="password")
    st.session_state.admin_ok = passcode_ok(entered, config.admin_passcode)
    return bool(st.session_state.admin_ok)


def _sidebar(config: AppConfig) -> bool:
    """Show what's connected. Returns whether this session may send for real."""
    with st.sidebar:
        st.subheader("Integrations")
        for label, ready in (
            ("Models (OpenRouter)", config.llm_configured),
            ("Slack", config.slack_configured),
            ("JIRA", config.jira_configured),
            ("LangSmith", config.langsmith_tracing and config.langsmith_api_key is not None),
        ):
            st.write(f"{':green-badge[connected]' if ready else ':gray-badge[off]'} {label}")
        live = _live_allowed(config)
        if live:
            st.warning("Live mode: approved runs post to Slack and JIRA.")
        else:
            st.info("Dry run: nothing is sent. You see exactly what would go out.")
        with st.expander("Severity legend"):
            st.dataframe(ui.SEVERITY_LEGEND, hide_index=True)
        if admin_overrides().differences(load_config()):
            st.caption("An admin changed some settings until the app restarts.")
        if _settings(config) != RunSettings.from_config(config):
            st.caption("Using custom agent settings for this session.")
        st.caption(f"Version {__version__}. Up to 5 runs per 10 minutes per visitor.")
    return live


def _pick_log() -> tuple[bytes, str] | None:
    """The chosen sample, upload or pasted text, as bytes and a file name."""
    source = st.radio("Log", ["Sample log", "Upload your own", "Paste text"], horizontal=True)
    if source == "Sample log":
        samples = _samples()
        name = st.selectbox("Sample", list(samples), format_func=lambda f: f"{f}: {samples[f]}")
        return ((SAMPLES / name).read_bytes(), name) if name else None
    if source == "Paste text":
        text = st.text_area(
            "Paste log lines (JSON lines and CSV work too)",
            height=220,
            max_chars=PASTE_MAX_CHARS,
            placeholder="2026-10-11T03:12:09Z ERROR payment-api Connection refused to db:5432",
        )
        return (text.encode("utf-8"), PASTED_NAME) if text.strip() else None
    upload = st.file_uploader(
        "Ops log: text, JSON lines, JSON, CSV, TSV or Parquet, optionally gzipped",
    )
    return (upload.getvalue(), upload.name) if upload is not None else None


# ------------------------------------------------------------------ running


def _progress(steps: list[RunStep], chart: Any, log: Any) -> Any:
    """A callback that lights up the graph and lists each step as it finishes."""

    def on_step(step: RunStep) -> None:
        steps.append(step)
        chart.graphviz_chart(ui.pipeline_dot(steps, running=True))
        log.write(f"{ui.LABELS.get(step.node, step.node)}: {ui.md(step.detail)}")

    return on_step


def _start(config: AppConfig, live: bool, data: bytes, filename: str) -> None:
    """Check, parse and run a log, then keep everything the results screen needs."""
    try:
        check_upload(filename, len(data), config.max_upload_mb)
        _session("limiter", RunLimiter()).check()
        _shared_limiter().check()
    except (RateLimitError, UploadError) as exc:
        st.error(ui.md(str(exc)))
        return
    if not config.llm_configured:
        st.error("This app needs OPENROUTER_API_KEY to analyse logs. See the README.")
        return
    chosen = _settings(config)
    live, note = ui.decide_live(chosen, live, st.session_state.get("admin_ok", False))
    settings = chosen.model_copy(update={"live": live})
    try:
        parsed = parse_upload(data, display_name(filename), settings.redaction)
        retriever = _session_retriever(config)
    except ReadError as exc:
        st.error(ui.md(str(exc)))
        return
    except openai.OpenAIError as exc:
        st.error(f"Couldn't index the runbooks ({type(exc).__name__}). Try again in a minute.")
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
        result = start_run(graph, context, run_id, _progress(steps, chart, st.container()))
        status.update(label=f"Finished {ui.md(parsed.name)}", state="complete", expanded=False)
    st.session_state.update(
        context=context, run_id=run_id, result=result, run_note=note, assistant=None, chat=[]
    )
    st.rerun()


def _answer(approved: bool) -> None:
    """Resume the paused run with the person's decision."""
    state = st.session_state
    with st.spinner("Sending..." if approved else "Recording the rejection..."):
        state.result = resume_run(state.graph, state.context, state.run_id, approved)


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
    for error in result.errors:
        st.warning(ui.md(error))


def _approval(result: RunResult) -> None:
    with st.container(border=True):
        st.subheader("Approval needed")
        st.write("Nothing has been sent yet. This is exactly what will go to Slack and JIRA.")
        _outbox(result)
        approve, reject, _ = st.columns([1, 1, 4])
        approve.button("Approve and send", type="primary", on_click=_answer, args=(True,))
        reject.button("Reject", on_click=_answer, args=(False,))


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
        st.button(
            "Draft a runbook (AI)", key=f"draft-{key}", on_click=_make_draft, args=(key, issue_id)
        )
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
    approve, discard, _ = st.columns([1, 1, 3])
    approve.button(
        "Approve",
        key=f"approve-{key}",
        on_click=_approve_draft,
        args=(key,),
        disabled=bool(draft.problems),
    )
    discard.button("Discard", key=f"discard-{key}", on_click=_discard_draft, args=(key,))


def _tabs(result: RunResult, context: RunContext) -> None:
    parsed = context.parsed
    tabs = st.tabs(
        ["Checklist", "Slack and JIRA", "Parsed log", "Patterns", "Runbooks", "Run log", "Cost"]
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
        st.caption(ui.md(parsed.note))
        if parsed.redactions:
            counts = ", ".join(f"{kind}: {n}" for kind, n in parsed.redactions.items())
            st.write(f"Redacted before any agent saw the log: {counts}")
        if parsed.column_mapping:
            st.write("Column mapping", parsed.column_mapping)
        if len(parsed.lines) > ui.MAX_TABLE_ROWS:
            st.caption(f"Showing the first {ui.MAX_TABLE_ROWS:,} of {len(parsed.lines):,}.")
        st.dataframe(ui.line_rows(parsed.lines), hide_index=True)
    with tabs[3]:
        st.dataframe(ui.pattern_rows(result.patterns), hide_index=True)
    with tabs[4]:
        st.dataframe(ui.source_rows(result.remediations), hide_index=True)
    with tabs[5]:
        st.dataframe(ui.pipeline_strip(result.steps), hide_index=True)
        st.dataframe(ui.run_log_rows(result.steps), hide_index=True)
    with tabs[6]:
        st.dataframe(ui.usage_rows(result.usage), hide_index=True)
        if result.trace_url:
            st.link_button("Open the trace", result.trace_url)


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


def _ask_assistant(config: AppConfig) -> None:
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
    """Answer a question about the current run and keep it in the chat history."""
    state = st.session_state
    result: RunResult = state.result
    context: RunContext = state.context
    reply = ask_about_run(
        question,
        [(q, a) for q, a in state.chat if a],
        result,
        {line.line_no: line.raw for line in context.parsed.lines},
        OpenRouterLLM(effective_config()),
        context.settings.token_budget,
    )
    state.chat.append((question, reply.answer or ""))
    if reply.error:
        state.chat_error = reply.error
    if reply.usage is not None:
        state.result = result.model_copy(
            update={"usage": merge_usage([*result.usage, reply.usage])}
        )


def _chat() -> None:
    """Sidebar chat about this run only (plan 17.4)."""
    chat: list[tuple[str, str]] = _session("chat", [])
    for question, answer in chat:
        st.markdown(f"**You:** {ui.md(question)}")
        st.markdown(ui.md(answer) if answer else "_No answer._")
    if error := st.session_state.pop("chat_error", None):
        st.warning(ui.md(error))
    with st.form("chat_form", clear_on_submit=True, border=False):
        question = st.text_input(
            "Ask about this run", max_chars=MAX_QUESTION_CHARS, placeholder="Why is ISS-1 critical?"
        )
        if st.form_submit_button("Ask") and question.strip():
            _ask_about_run(question.strip())
            st.rerun()


def _assistant(config: AppConfig) -> None:
    """Sidebar: a chat about this run (17.4), and Slack and JIRA updates since it (17.2)."""
    result: RunResult = st.session_state.result
    with st.sidebar:
        st.subheader("Assistant")
        _chat()
        st.markdown("**Since the run**")
        if not (result.slack.sent or any(t.sent for t in result.tickets)):
            st.caption("After a live run posts to Slack or JIRA, ask what has happened since.")
            return
        if not st.session_state.get("admin_ok"):
            st.caption("Enter the admin passcode above to ask about Slack and JIRA.")
            return
        st.button("What's happened since?", on_click=_ask_assistant, args=(config,))
        answer: AssistantResult | None = st.session_state.get("assistant")
        if answer is None:
            return
        if answer.error:
            st.warning(ui.md(answer.error))
        if answer.answer is not None:
            st.markdown(ui.md(answer.answer.summary))
            st.markdown("\n".join(f"- {ui.md(line)}" for line in answer.answer.updates))
            if answer.answer.next_step:
                st.markdown(f"**Next:** {ui.md(answer.answer.next_step)}")
        if answer.sources:
            st.caption("Read: " + ui.md("; ".join(answer.sources)))


# ------------------------------------------------------------------ page


def main() -> None:
    """Render the page."""
    st.set_page_config(page_title="Incident Suite", page_icon=":rotating_light:", layout="wide")
    config = effective_config()
    live = _sidebar(config)
    st.title("Incident Suite")
    st.caption(
        "Upload an ops log. Agents find the problems, suggest fixes with sources, write a "
        "checklist, and prepare the Slack post and JIRA tickets for your approval."
    )
    picked = _pick_log()
    if st.button("Analyse", type="primary", disabled=picked is None) and picked is not None:
        _start(config, live, *picked)
    if "result" in st.session_state:
        _assistant(config)
        _results(st.session_state.result, st.session_state.context)


main()
