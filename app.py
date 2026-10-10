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
from incident_suite.config import AppConfig, load_config
from incident_suite.graph import (
    RunContext,
    build_graph,
    connect_services,
    resume_run,
    start_run,
)
from incident_suite.parsing.readers import ReadError, parse_upload
from incident_suite.rag.retriever import RunbookRetriever
from incident_suite.rag.runbooks import load_runbooks
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
    LogFormat,
    RunResult,
    RunSettings,
    RunStep,
)
from incident_suite.telemetry import enable_tracing, new_run_id, trace_link

SAMPLES = Path(__file__).resolve().parent / "sample_logs"
TEXT_FORMATS = {
    LogFormat.PLAIN,
    LogFormat.NGINX_ACCESS,
    LogFormat.NGINX_ERROR,
    LogFormat.SYSLOG,
    LogFormat.KUBERNETES,
}


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


def _session(key: str, default: Any) -> Any:
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


# ------------------------------------------------------------------ sidebar and input


def _live_allowed(config: AppConfig) -> bool:
    """Live sending, if it's on, needs the admin passcode when one is set."""
    if not config.live_integrations:
        return False
    if config.admin_passcode is None:
        return True
    entered = st.text_input("Admin passcode for live sending", type="password")
    return passcode_ok(entered, config.admin_passcode)


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
        st.caption(f"Version {__version__}. Up to 5 runs per 10 minutes per visitor.")
    return live


def _pick_log() -> tuple[bytes, str] | None:
    """The chosen sample or upload, as bytes and its file name."""
    source = st.radio("Log", ["Sample log", "Upload your own"], horizontal=True)
    if source == "Sample log":
        samples = _samples()
        name = st.selectbox("Sample", list(samples), format_func=lambda f: f"{f}: {samples[f]}")
        return ((SAMPLES / name).read_bytes(), name) if name else None
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
    settings = RunSettings.from_config(config).model_copy(update={"live": live})
    try:
        parsed = parse_upload(data, display_name(filename), settings.redaction)
        retriever = _retriever(config.embedding_model)
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
    st.session_state.update(context=context, run_id=run_id, result=result)
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
    structured = result.log_format not in TEXT_FORMATS
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


# ------------------------------------------------------------------ page


def main() -> None:
    """Render the page."""
    st.set_page_config(page_title="Incident Suite", page_icon=":rotating_light:", layout="wide")
    config = load_config()
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
        _results(st.session_state.result, st.session_state.context)


main()
