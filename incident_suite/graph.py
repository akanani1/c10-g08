"""LangGraph orchestrator.

    log_reader -> remediation -> [cookbook || jira_plan*] -> notify_plan -> approval** -> send
                     ^      |
                     +-widen+   (at most one retry per issue)

    *  only when an issue reaches the ticket severity (critical by default)
    ** pauses with interrupt() only when something would really be sent

The graph state holds what the run produced. The inputs that don't change during a run (the
parsed log, the run settings and the model, runbook, JIRA and Slack services) travel in the
LangGraph runtime context instead, so the 50k-line log isn't copied into every checkpoint.
Every node appends a :class:`RunStep`, which feeds the pipeline strip and the run log.

:func:`connect_services` gives the context real Slack and JIRA clients only when the run is
live and the credentials are there. Otherwise the run is a dry run and nothing is sent.
"""

from __future__ import annotations

import argparse
import operator
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from incident_suite import state as state_models
from incident_suite.agents import StructuredLLM
from incident_suite.agents.cookbook import synthesize
from incident_suite.agents.jira_agent import (
    TicketDraft,
    TicketTracker,
    link_back,
    plan_tickets,
    remember_thread,
    send_tickets,
)
from incident_suite.agents.log_reader import classify, widen_context
from incident_suite.agents.notification import Messenger, plan_notification, send_notification
from incident_suite.agents.remediation import remediate
from incident_suite.config import AppConfig
from incident_suite.integrations.jira_client import JiraClient
from incident_suite.integrations.slack_client import SlackClient
from incident_suite.parsing.grouping import group_lines
from incident_suite.rag.retriever import RunbookRetriever
from incident_suite.state import (
    AgentUsage,
    ApprovalStatus,
    Checklist,
    GroupedLog,
    Issue,
    LogLine,
    NotificationPlan,
    ParsedLog,
    Remediation,
    RunResult,
    RunSettings,
    RunStep,
    SlackOutcome,
    StepStatus,
    TicketAction,
    TicketOutcome,
)
from incident_suite.telemetry import UsageMeter, merge_usage

WIDE_WINDOW = 10  # lines either side of each evidence line on the "more context" retry

# The only of our types a checkpoint may turn back into objects when a paused run resumes.
CHECKPOINT_TYPES = tuple(
    value
    for value in vars(state_models).values()
    if isinstance(value, type) and value.__module__ == state_models.__name__
)


@dataclass(frozen=True)
class RunContext:
    """Inputs fixed for the whole run. Not checkpointed; pass the same one when resuming."""

    parsed: ParsedLog
    settings: RunSettings
    llm: StructuredLLM
    retriever: RunbookRetriever
    tracker: TicketTracker | None = None
    messenger: Messenger | None = None
    trace_id: UUID | None = None
    trace_url: str | None = None


class RunState(TypedDict, total=False):
    """What the run has produced so far. Lists with ``operator.add`` collect from every node."""

    run_id: str
    grouped: GroupedLog
    issues: list[Issue]
    evidence: dict[str, list[LogLine]]
    remediations: dict[str, Remediation]
    retry_ids: list[str]
    retried: list[str]
    checklist: Checklist
    tickets: list[TicketOutcome]
    notification: NotificationPlan
    approval: ApprovalStatus
    slack: SlackOutcome
    errors: Annotated[list[str], operator.add]
    steps: Annotated[list[RunStep], operator.add]
    usage: Annotated[list[AgentUsage], operator.add]


def _step(node: str, started: float, detail: str, status: StepStatus = StepStatus.DONE) -> RunStep:
    return RunStep(node=node, status=status, seconds=time.perf_counter() - started, detail=detail)


def _tokens_used(state: RunState) -> int:
    return sum(u.input_tokens + u.output_tokens for u in state.get("usage", []))


def _wants_tickets(issues: Sequence[Issue], settings: RunSettings) -> bool:
    return any(issue.severity.rank >= settings.jira.min_severity.rank for issue in issues)


def _sendable(tickets: Sequence[TicketOutcome]) -> bool:
    return any(t.action is not TicketAction.SKIPPED_OVER_CAP and t.error is None for t in tickets)


# ------------------------------------------------------------------ nodes


def log_reader(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:  # noqa: ARG001 - first node, reads only the context
    """Group the log, classify the patterns into issues, and keep each issue's evidence."""
    started = time.perf_counter()
    ctx = runtime.context
    grouped = group_lines(ctx.parsed.lines, ctx.settings.log_reader.context_lines)
    meter = UsageMeter("log_reader", ctx.settings.log_reader.model)
    result = classify(ctx.parsed, grouped, ctx.settings, ctx.llm, meter)
    by_number = {line.line_no: line for line in ctx.parsed.lines}
    evidence = {
        issue.id: [by_number[n] for n in issue.evidence_lines if n in by_number]
        for issue in result.issues
    }
    detail = (
        f"{len(ctx.parsed.lines)} lines -> {len(grouped.patterns)} patterns -> "
        f"{len(result.issues)} issues"
    )
    return {
        "grouped": grouped,
        "issues": result.issues,
        "evidence": evidence,
        "remediations": {},
        "retry_ids": [],
        "retried": [],
        "errors": result.errors,
        "usage": [meter.usage],
        "steps": [_step("log_reader", started, detail)],
    }


def remediation(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Find a fix per issue; on the second pass only the issues sent back for more context."""
    started = time.perf_counter()
    ctx = runtime.context
    targets = set(state["retry_ids"]) or {issue.id for issue in state["issues"]}
    fixes = dict(state["remediations"])
    meter = UsageMeter("remediation", ctx.settings.remediation.model)
    errors: list[str] = []
    retry: list[str] = []
    for issue in (i for i in state["issues"] if i.id in targets):
        if _tokens_used(state) + meter.tokens >= ctx.settings.token_budget:
            errors.append(f"Token budget reached; no fix for {issue.id}.")
            continue
        result = remediate(
            issue,
            state["evidence"][issue.id],
            retriever=ctx.retriever,
            settings=ctx.settings,
            llm=ctx.llm,
            meter=meter,
        )
        if result.remediation is not None:
            fixes[issue.id] = result.remediation
        if result.error:
            errors.append(result.error)
        if result.needs_more_context and issue.id not in state["retried"]:
            retry.append(issue.id)
    detail = f"{len(targets)} issue(s); {len(retry)} sent back for more context"
    return {
        "remediations": fixes,
        "retry_ids": retry,
        "errors": errors,
        "usage": [meter.usage],
        "steps": [_step("remediation", started, detail)],
    }


def widen(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """The feedback loop: give the issues Remediation wasn't sure about a wider window."""
    started = time.perf_counter()
    evidence = dict(state["evidence"])
    issues = {issue.id: issue for issue in state["issues"]}
    for issue_id in state["retry_ids"]:
        wider = widen_context(runtime.context.parsed, issues[issue_id], WIDE_WINDOW)
        if wider:
            evidence[issue_id] = wider
    detail = "More context for " + ", ".join(state["retry_ids"])
    return {
        "evidence": evidence,
        "retried": state["retried"] + state["retry_ids"],
        "steps": [_step("log_reader", started, detail)],
    }


def cookbook(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Turn the fixes into the do now / this week / prevent checklist."""
    started = time.perf_counter()
    ctx = runtime.context
    meter = UsageMeter("cookbook", ctx.settings.cookbook.model)
    result = synthesize(
        state["issues"], list(state["remediations"].values()), ctx.settings, ctx.llm, meter
    )
    items = sum(
        len(group)
        for group in (result.checklist.do_now, result.checklist.this_week, result.checklist.prevent)
    )
    return {
        "checklist": result.checklist,
        "errors": result.errors,
        "usage": [meter.usage],
        "steps": [_step("cookbook", started, f"{items} checklist items")],
    }


def jira_plan(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Decide create / comment / regression per critical issue. Read-only."""
    started = time.perf_counter()
    ctx = runtime.context
    tickets = plan_tickets(
        state["issues"], ctx.settings, ctx.tracker if ctx.settings.live else None
    )
    errors = [f"JIRA ({t.issue_id}): {t.error}" for t in tickets if t.error]
    detail = ", ".join(f"{t.issue_id}: {t.action}" for t in tickets)
    return {"tickets": tickets, "errors": errors, "steps": [_step("jira_plan", started, detail)]}


def notify_plan(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Write the Slack summary and thread, after the checklist and ticket plan are ready."""
    started = time.perf_counter()
    ctx = runtime.context
    steps = []
    if "tickets" not in state:
        steps.append(_step("jira_plan", started, "No issue needs a ticket", StepStatus.SKIPPED))
    plan = plan_notification(
        state["issues"],
        list(state["remediations"].values()),
        state.get("checklist", Checklist()),
        state.get("tickets", []),
        ctx.parsed.name,
        state["run_id"],
    )
    if plan.post and ctx.trace_url is not None:
        plan = plan.model_copy(update={"trace_url": ctx.trace_url})
    steps.append(_step("notification", started, plan.reason))
    return {"notification": plan, "steps": steps}


def approval(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Pause for a person when something would really be sent."""
    started = time.perf_counter()
    ctx = runtime.context
    jira_ready = ctx.tracker is not None and _sendable(state.get("tickets", []))
    slack_ready = ctx.messenger is not None and state["notification"].post
    if not ctx.settings.live or not (jira_ready or slack_ready):
        reason = "Dry run: nothing sent" if not ctx.settings.live else "Nothing to send"
        return {
            "approval": ApprovalStatus.NOT_NEEDED,
            "steps": [_step("approval", started, reason, StepStatus.SKIPPED)],
        }
    if not ctx.settings.require_approval:
        return {
            "approval": ApprovalStatus.APPROVED,
            "steps": [_step("approval", started, "Approval not required")],
        }
    decision = interrupt({"run_id": state["run_id"], "question": "Send to Slack and JIRA?"})
    approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    detail = "Approved in app" if approved else "Rejected in app: nothing sent"
    return {"approval": status, "steps": [_step("approval", started, detail)]}


def _drafts(state: RunState) -> dict[str, TicketDraft]:
    patterns = {p.signature_hash: p for p in state["grouped"].patterns}
    fixes = state["remediations"]
    return {
        issue.id: TicketDraft(
            issue=issue,
            remediation=fixes.get(issue.id),
            pattern=patterns.get(issue.signature_hash or ""),
            run_id=state["run_id"],
        )
        for issue in state["issues"]
    }


def send(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """JIRA first, so the Slack post can carry the ticket links; then link the tickets back."""
    started = time.perf_counter()
    ctx = runtime.context
    planned = state.get("tickets", [])
    tickets = planned
    if ctx.tracker is not None:
        tickets = send_tickets(planned, _drafts(state), ctx.tracker)
    slack = SlackOutcome()
    if ctx.messenger is not None:
        slack = send_notification(state["notification"], tickets, ctx.messenger)
    already_shown = {(t.issue_id, t.error) for t in planned}
    errors = [
        f"JIRA ({t.issue_id}): {t.error}"
        for t in tickets
        if t.error and (t.issue_id, t.error) not in already_shown
    ]
    if slack.error:
        errors.append(f"Slack: {slack.error}")
    links = [
        (url, f"{title} ({state['run_id']})")
        for url, title in (
            (slack.permalink, "Slack: incident summary"),
            (ctx.trace_url, "LangSmith trace"),
        )
        if url is not None
    ]
    if ctx.tracker is not None and links:
        errors += link_back(tickets, links, ctx.tracker)
    if ctx.tracker is not None and slack.channel and slack.ts:
        errors += remember_thread(tickets, slack.channel, slack.ts, ctx.tracker)
    sent = sum(1 for t in tickets if t.sent)
    detail = f"{sent} ticket action(s); Slack {'posted' if slack.sent else 'not posted'}"
    return {
        "tickets": tickets,
        "slack": slack,
        "errors": errors,
        "steps": [_step("send", started, detail)],
    }


# ------------------------------------------------------------------ routing


def _after_remediation(state: RunState, runtime: Runtime[RunContext]) -> list[str]:
    if state["retry_ids"]:
        return ["widen"]
    if _wants_tickets(state["issues"], runtime.context.settings):
        return ["cookbook", "jira_plan"]
    return ["cookbook"]


def _after_approval(state: RunState) -> str:
    return "send" if state["approval"] is ApprovalStatus.APPROVED else END


def build_graph() -> CompiledStateGraph[RunState, RunContext, RunState, RunState]:
    """The compiled graph with an in-memory checkpointer. Keep one per app session."""
    graph = StateGraph(RunState, context_schema=RunContext)
    graph.add_node("log_reader", log_reader)
    graph.add_node("remediation", remediation)
    graph.add_node("widen", widen)
    graph.add_node("cookbook", cookbook)
    graph.add_node("jira_plan", jira_plan)
    graph.add_node("notify_plan", notify_plan)
    graph.add_node("approval", approval)
    graph.add_node("send", send)
    graph.add_edge(START, "log_reader")
    graph.add_edge("log_reader", "remediation")
    graph.add_conditional_edges(
        "remediation", _after_remediation, ["widen", "cookbook", "jira_plan"]
    )
    graph.add_edge("widen", "remediation")
    graph.add_edge("cookbook", "notify_plan")
    graph.add_edge("jira_plan", "notify_plan")
    graph.add_edge("notify_plan", "approval")
    graph.add_conditional_edges("approval", _after_approval, ["send", END])
    graph.add_edge("send", END)
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return graph.compile(checkpointer=InMemorySaver(serde=serde))


# ------------------------------------------------------------------ running


def connect_services(
    config: AppConfig, settings: RunSettings
) -> tuple[JiraClient | None, SlackClient | None]:
    """Real JIRA and Slack clients for a live run, each only if its credentials are all set.

    A dry run gets neither, so nothing can be sent however the rest of the run goes.
    """
    if not settings.live:
        return None, None
    tracker = JiraClient(config) if config.jira_configured else None
    messenger = SlackClient(config) if config.slack_configured else None
    return tracker, messenger


OnStep = Callable[[RunStep], None]


def _config(run_id: str, trace_id: UUID | None = None) -> RunnableConfig:
    """Checkpoint thread, plus a name and tags so the run is easy to find in LangSmith."""
    config = RunnableConfig(
        configurable={"thread_id": run_id},
        run_name=f"TriageGraph {run_id}",
        tags=["incident-suite"],
        metadata={"run_id": run_id},
    )
    if trace_id is not None:
        config["run_id"] = trace_id
    return config


def _drive(
    graph: CompiledStateGraph[RunState, RunContext, RunState, RunState],
    graph_input: RunState | Command[Any],
    context: RunContext,
    run_id: str,
    on_step: OnStep | None,
    trace_id: UUID | None = None,
) -> None:
    """Run the graph, telling ``on_step`` about each step as soon as its node finishes."""
    for update in graph.stream(
        graph_input, _config(run_id, trace_id), context=context, stream_mode="updates"
    ):
        for node_update in update.values():
            if on_step is not None and isinstance(node_update, dict):
                for step in node_update.get("steps", []):
                    on_step(step)


def start_run(
    graph: CompiledStateGraph[RunState, RunContext, RunState, RunState],
    context: RunContext,
    run_id: str,
    on_step: OnStep | None = None,
) -> RunResult:
    """Run until the end, or until the approval gate pauses."""
    _drive(graph, RunState(run_id=run_id), context, run_id, on_step, context.trace_id)
    return result_of(graph, context, run_id)


def resume_run(
    graph: CompiledStateGraph[RunState, RunContext, RunState, RunState],
    context: RunContext,
    run_id: str,
    approved: bool,
    on_step: OnStep | None = None,
) -> RunResult:
    """Answer the approval gate and finish the run."""
    resume: Command[Any] = Command(resume={"approved": approved})
    _drive(graph, resume, context, run_id, on_step)
    return result_of(graph, context, run_id)


def result_of(
    graph: CompiledStateGraph[RunState, RunContext, RunState, RunState],
    context: RunContext,
    run_id: str,
) -> RunResult:
    """The run as it stands: finished, or paused at the approval gate."""
    snapshot = graph.get_state(_config(run_id))
    state: dict[str, Any] = snapshot.values
    paused = bool(snapshot.next)
    issues = state.get("issues", [])
    fixes = state.get("remediations", {})
    parsed = context.parsed
    return RunResult(
        run_id=run_id,
        log_name=parsed.name,
        log_fingerprint=parsed.fingerprint,
        log_format=parsed.format,
        line_count=len(parsed.lines),
        redactions=parsed.redactions,
        patterns=state["grouped"].patterns if "grouped" in state else [],
        issues=issues,
        remediations=[fixes[i.id] for i in issues if i.id in fixes],
        checklist=state.get("checklist", Checklist()),
        tickets=state.get("tickets", []),
        notification=state.get("notification"),
        approval=ApprovalStatus.PENDING
        if paused
        else state.get("approval", ApprovalStatus.NOT_NEEDED),
        slack=state.get("slack", SlackOutcome()),
        usage=merge_usage(state.get("usage", [])),
        steps=state.get("steps", []),
        trace_url=context.trace_url,
        dry_run=not context.settings.live,
        errors=[*parsed.warnings, *state.get("errors", [])],
    )


# ------------------------------------------------------------------ command line


def _print(result: RunResult) -> None:
    print(f"\n{result.log_name}: {result.line_count} lines, {len(result.patterns)} patterns")
    for step in result.steps:
        print(f"  {step.node:<13} {step.status:<8} {step.seconds:6.1f}s  {step.detail}")
    fixes = {fix.issue_id: fix for fix in result.remediations}
    for issue in result.issues:
        print(f"\n{issue.id} [{issue.severity}] {issue.title} ({issue.affected_service or '-'})")
        print(f"  evidence: {', '.join(f'L{n}' for n in issue.evidence_lines)}")
        if issue.id in fixes:
            fix = fixes[issue.id]
            print(f"  root cause: {fix.root_cause}")
            for n, fix_step in enumerate(fix.steps, start=1):
                print(f"  {n}. {fix_step}")
            print(f"  sources: {', '.join(s.runbook for s in fix.sources) or '-'}")
    if result.notification is not None:
        print(f"\nSlack: {result.notification.reason}")
        if result.slack.permalink:
            print(f"  posted: {result.slack.permalink}")
    for ticket in result.tickets:
        sent = f" ({ticket.key}, sent)" if ticket.sent else ""
        print(f"JIRA: {ticket.issue_id} -> {ticket.action}{sent}")
    for error in result.errors:
        print(f"error: {error}")
    cost = sum(u.cost_usd for u in result.usage)
    tokens = sum(u.input_tokens + u.output_tokens for u in result.usage)
    ending = "Dry run: nothing was sent." if result.dry_run else f"Approval: {result.approval}."
    print(f"\n{tokens} tokens, ${cost:.4f}. {ending}")
    if result.trace_url:
        print(f"Trace: {result.trace_url}")


def _ask(result: RunResult) -> bool:
    """Show what a paused run would send and ask on the terminal."""
    if result.notification is not None and result.notification.post:
        print("\n--- Slack summary ---\n" + result.notification.summary)
    for ticket in result.tickets:
        print(f"--- JIRA: {ticket.issue_id} -> {ticket.action} {ticket.key or ''}")
    return input("\nSend to Slack and JIRA? [y/N] ").strip().lower() == "y"


def main(argv: list[str] | None = None) -> int:
    """``python -m incident_suite.graph <log file> [--live]``: one run with the real models.

    It's a dry run unless ``--live`` is given and ``LIVE_INTEGRATIONS=true``. A live run asks on
    the terminal before anything is sent, like the approval panel in the app.
    """
    from incident_suite.agents import OpenRouterLLM, make_embeddings  # noqa: PLC0415
    from incident_suite.config import load_config  # noqa: PLC0415
    from incident_suite.parsing.readers import ReadError, parse_upload  # noqa: PLC0415
    from incident_suite.rag.runbooks import load_runbooks  # noqa: PLC0415
    from incident_suite.telemetry import enable_tracing, new_run_id, trace_link  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m incident_suite.graph")
    parser.add_argument("path", type=Path, help="log file to analyse")
    parser.add_argument("--live", action="store_true", help="send to Slack and JIRA")
    args = parser.parse_args(argv)
    config = load_config()
    if not config.llm_configured:
        print("error: OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 1
    if args.live and not config.live_integrations:
        print("error: --live needs LIVE_INTEGRATIONS=true", file=sys.stderr)
        return 1
    try:
        parsed = parse_upload(args.path.read_bytes(), args.path.name)
    except (OSError, ReadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    settings = RunSettings.from_config(config).model_copy(update={"live": args.live})
    tracker, messenger = connect_services(config, settings)
    trace_id = uuid4() if enable_tracing(config) else None
    context = RunContext(
        parsed=parsed,
        settings=settings,
        llm=OpenRouterLLM(config),
        retriever=RunbookRetriever(load_runbooks(), make_embeddings(config)),
        tracker=tracker,
        messenger=messenger,
        trace_id=trace_id,
        trace_url=trace_link(config, trace_id) if trace_id else None,
    )
    graph, run_id = build_graph(), new_run_id()
    try:
        result = start_run(graph, context, run_id)
        if result.approval is ApprovalStatus.PENDING:
            result = resume_run(graph, context, run_id, approved=_ask(result))
    finally:
        for client in (tracker, messenger):
            if client is not None:
                client.close()
    _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
