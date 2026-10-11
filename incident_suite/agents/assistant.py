"""Assistant: Slack and JIRA updates (17.2), runbook drafts (17.3) and a chat about a run (17.4).

**Chat about the run.** A small tool loop: each turn the model either picks one read-only tool
(issues, evidence, fix, checklist, tickets, costs) or answers. Tool results come back inside
``<tool_result>`` tags as data. After ``MAX_TOOL_CALLS`` it has to answer. It only sees this
run, and it cites line numbers from the evidence.

**Runbook drafts.** When an issue still has no good runbook after its retry, the assistant can
draft one. The model fills in the template's fields; our code writes the markdown, so a draft
always has the right shape, and it must pass the same checks as our own runbooks. A draft is
labelled "AI draft, not reviewed". It only ever lives in the person's session and their download.

**Updates since the run:**

A small, read-only LangGraph agent with three steps:

    read_slack -> read_jira -> summarise

- **read_slack** reads the replies in this run's Slack thread. For a ticket that was seen
  before, it also reads the original incident's thread, which we stored on the ticket as the
  JIRA issue property ``incident_suite.slack`` when we first posted.
- **read_jira** reads status, assignee and the latest comments of this run's tickets. Which
  tickets, and which fields, is fixed in code: the model never writes a query.
- **summarise** asks the cheap model for a short summary. Everything it reads is redacted and
  wrapped in tags, as data and never as instructions.

Without a run (a new session, or after the app restarts), the same graph reads one earlier
incident the person picks from the latest TriageGraph tickets in JIRA: that ticket, and the
Slack thread stored on it.

The readers are protocols; the Slack and JIRA clients implement them, and tests use fakes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

import yaml
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from incident_suite.agents import LLMError, StructuredLLM
from incident_suite.parsing.redact import Redactor
from incident_suite.prompts import load_prompt
from incident_suite.rag.runbooks import Runbook, check_runbook, parse_runbook
from incident_suite.state import (
    AgentUsage,
    Category,
    Issue,
    Remediation,
    RunResult,
    TicketAction,
)
from incident_suite.telemetry import UsageMeter

MODEL = "openai/gpt-5-nano"  # the cheap model on the allow-list
TASK_UPDATES = "<task>updates</task>\n"
TASK_RUNBOOK = "<task>runbook</task>\n"
TASK_CHAT = "<task>chat</task>\n"
MAX_TOOL_CALLS = 4
MAX_QUESTION_CHARS = 500
MAX_HISTORY = 3
DRAFT_LABEL = "AI draft, not reviewed"
MAX_EVIDENCE_LINES = 20
MAX_CHARS_PER_MESSAGE = 1_000
_TAG = re.compile(
    r"</?\s*(slack_thread|jira_ticket|run|task|issue|evidence|question|tool_result|earlier)\b[^>]*>",
    re.IGNORECASE,
)


class ReaderError(RuntimeError):
    """Slack or JIRA couldn't be read. The message is safe to show."""


@dataclass(frozen=True)
class ThreadMessage:
    """One reply in a Slack thread."""

    author: str
    text: str
    ts: str


@dataclass(frozen=True)
class TicketUpdate:
    """Where a JIRA ticket stands now."""

    key: str
    status: str
    assignee: str
    comments: tuple[str, ...] = ()


class ThreadReader(Protocol):
    """What the assistant needs from Slack."""

    def replies(self, channel: str, ts: str, limit: int = ...) -> list[ThreadMessage]:
        """The replies in a thread, without the message that started it."""
        ...


class TicketReader(Protocol):
    """What the assistant needs from JIRA."""

    def updates(self, key: str) -> TicketUpdate:
        """Status, assignee and the latest comments of one of our tickets."""
        ...

    def slack_thread(self, key: str) -> tuple[str, str] | None:
        """The Slack ``(channel, ts)`` stored on a ticket when it was first posted."""
        ...


@dataclass(frozen=True)
class RecentTicket:
    """One of our earlier incidents in JIRA, for the picker."""

    key: str
    summary: str
    status: str
    created: str


class AssistantAnswer(BaseModel):
    """What the model returns."""

    summary: str = Field(description="Two or three sentences: what changed since the run")
    updates: list[str] = Field(
        default_factory=list, max_length=8, description="One line per notable update"
    )
    next_step: str | None = Field(default=None, description="One suggested next step, if any")


@dataclass
class AssistantResult:
    """The answer, or why there isn't one, plus what was read and what it cost."""

    answer: AssistantAnswer | None = None
    error: str | None = None
    sources: list[str] = field(default_factory=list)
    usage: AgentUsage | None = None


@dataclass(frozen=True)
class AssistantInput:
    """Everything one question needs. Kept out of the graph state, like the run's context."""

    result: RunResult | None
    threads: ThreadReader | None
    tickets: TicketReader | None
    llm: StructuredLLM
    redactor: Redactor
    token_budget: int
    progress: Callable[[str], None] | None = None
    keys: tuple[str, ...] = ()  # earlier incidents to read, when there's no run

    def say(self, message: str) -> None:
        """Tell the person what's happening, if anyone is listening."""
        if self.progress is not None:
            self.progress(message)


class _State(TypedDict, total=False):
    blocks: list[str]
    sources: list[str]
    problems: list[str]
    answer: AssistantAnswer | None
    error: str | None
    usage: AgentUsage


def _escape(text: str) -> str:
    return _TAG.sub("[tag]", text)


def _thread_block(label: str, messages: Sequence[ThreadMessage], redactor: Redactor) -> str:
    if not messages:
        lines = ["No replies yet."]
    else:
        lines = [
            f"{m.author}: {_escape(redactor.redact(m.text[:MAX_CHARS_PER_MESSAGE]))}"
            for m in messages
        ]
    return f'<slack_thread name="{label}">\n' + "\n".join(lines) + "\n</slack_thread>"


def _ticket_block(update: TicketUpdate, redactor: Redactor) -> str:
    comments = "\n".join(
        f"- {_escape(redactor.redact(c[:MAX_CHARS_PER_MESSAGE]))}" for c in update.comments
    )
    return (
        f'<jira_ticket key="{update.key}">\nstatus: {_escape(update.status)}\n'
        f"assignee: {_escape(update.assignee)}\n"
        f"latest comments:\n{comments or '- none'}\n</jira_ticket>"
    )


def _threads_to_read(data: AssistantInput) -> list[tuple[str, str, str]]:
    """``(label, channel, ts)`` for this run's thread and each seen-before ticket's thread.

    Without a run, the thread stored on each picked ticket.
    """
    result = data.result
    threads = []
    seen_before = list(data.keys)
    if result is not None:
        if result.slack.channel and result.slack.ts:
            threads.append(("this run", result.slack.channel, result.slack.ts))
        seen_before += [t.key for t in result.tickets if t.action is TicketAction.COMMENT and t.key]
    if data.tickets is not None:
        for key in seen_before:
            try:
                stored = data.tickets.slack_thread(key)
            except ReaderError:
                continue
            if stored is not None and stored[1] not in {t[2] for t in threads}:
                threads.append((f"first seen in {key}", *stored))
    return threads


def _tickets_to_read(data: AssistantInput) -> list[str]:
    run_keys = [t.key for t in data.result.tickets if t.sent and t.key] if data.result else []
    return [*run_keys, *(k for k in data.keys if k not in run_keys)]


def _graph(data: AssistantInput) -> Any:
    def read_slack(state: _State) -> dict[str, Any]:
        reader = data.threads
        if reader is None:
            return {}
        blocks, sources, problems = list(state["blocks"]), list(state["sources"]), []
        for label, channel, ts in _threads_to_read(data):
            data.say(f"Reading the Slack thread ({label})")
            try:
                messages = reader.replies(channel, ts)
            except ReaderError as exc:
                problems.append(f"Slack ({label}): {exc}")
                continue
            blocks.append(_thread_block(label, messages, data.redactor))
            sources.append(f"Slack thread ({label}), {len(messages)} replies")
        return {"blocks": blocks, "sources": sources, "problems": [*state["problems"], *problems]}

    def read_jira(state: _State) -> dict[str, Any]:
        reader = data.tickets
        if reader is None:
            return {}
        blocks, sources, problems = list(state["blocks"]), list(state["sources"]), []
        for key in _tickets_to_read(data):
            data.say(f"Reading {key} in JIRA")
            try:
                update = reader.updates(key)
            except ReaderError as exc:
                problems.append(f"JIRA ({key}): {exc}")
                continue
            blocks.append(_ticket_block(update, data.redactor))
            sources.append(f"{key}: {update.status}")
        return {"blocks": blocks, "sources": sources, "problems": [*state["problems"], *problems]}

    def summarise(state: _State) -> dict[str, Any]:
        meter = UsageMeter("assistant", MODEL)
        if not state["blocks"]:
            reason = "; ".join(state["problems"]) or "Nothing to read for this run."
            return {"answer": None, "error": reason, "usage": meter.usage}
        data.say("Summarising what changed")
        if data.result is not None:
            opening = f'<run id="{data.result.run_id}" log="{_escape(data.result.log_name)}">'
        else:
            opening = f'<run id="none" earlier_incident="{" ".join(data.keys)}">'
        user = opening + "\n" + "\n\n".join(state["blocks"]) + "\n</run>"
        started = time.perf_counter()
        try:
            reply = data.llm(MODEL, load_prompt("assistant"), TASK_UPDATES + user, AssistantAnswer)
        except LLMError as exc:
            meter.record_failure(exc, time.perf_counter() - started)
            return {"answer": None, "error": str(exc), "usage": meter.usage}
        meter.record(reply)
        return {"answer": reply.parsed, "error": None, "usage": meter.usage}

    graph = StateGraph(_State)
    graph.add_node("read_slack", read_slack)
    graph.add_node("read_jira", read_jira)
    graph.add_node("summarise", summarise)
    graph.add_edge(START, "read_slack")
    graph.add_edge("read_slack", "read_jira")
    graph.add_edge("read_jira", "summarise")
    graph.add_edge("summarise", END)
    return graph.compile()


def latest_updates(data: AssistantInput) -> AssistantResult:
    """Read this run's (or the picked incident's) Slack threads and tickets, and summarise."""
    spent = sum(u.input_tokens + u.output_tokens for u in data.result.usage) if data.result else 0
    if spent >= data.token_budget:
        return AssistantResult(error="This run has used its token budget.")
    if data.threads is None and data.tickets is None:
        return AssistantResult(error="Slack and JIRA aren't connected.")
    state = _graph(data).invoke(_State(blocks=[], sources=[], problems=[]))
    return AssistantResult(
        answer=state.get("answer"),
        error=state.get("error"),
        sources=state.get("sources", []),
        usage=state.get("usage"),
    )


# ------------------------------------------------------------------ runbook drafts


class RunbookFields(BaseModel):
    """What the model fills in. Our code turns it into the runbook template."""

    title: str = Field(min_length=3, max_length=120)
    signals: list[str] = Field(min_length=1, max_length=5, description="Log lines that show it")
    symptoms: list[str] = Field(min_length=1, max_length=6)
    likely_causes: list[str] = Field(min_length=1, max_length=6)
    diagnose: list[str] = Field(min_length=1, max_length=6)
    fix_now: list[str] = Field(min_length=1, max_length=6)
    prevent: list[str] = Field(min_length=1, max_length=6)
    references: list[str] = Field(default_factory=list, max_length=4)


@dataclass
class DraftResult:
    """A rendered draft and whether it passed the runbook checks."""

    text: str = ""
    runbook: Runbook | None = None
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    usage: AgentUsage | None = None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_runbook(fields: RunbookFields, issue: Issue) -> str:
    """The draft as a runbook file: front matter, then the template's sections."""
    suffix = (issue.signature_hash or issue.id.lower())[:8]
    meta = {
        "title": _one_line(fields.title),
        "id": f"draft-{issue.category.value.replace('_', '-')}-{suffix}",
        "status": DRAFT_LABEL,
        "categories": [issue.category.value],
        "typical_severity": issue.severity.value,
        "services": [issue.affected_service] if issue.affected_service else [],
        "signals": [_one_line(signal) for signal in fields.signals],
    }
    sections = [
        ("Symptoms", fields.symptoms, "-"),
        ("Likely causes", fields.likely_causes, "1."),
        ("Diagnose", fields.diagnose, "1."),
        ("Fix now", fields.fix_now, "1."),
        ("Prevent", fields.prevent, "-"),
        (
            "References",
            fields.references or ["None yet. Add links once this has been reviewed."],
            "-",
        ),
    ]
    body = [f"# {meta['title']}", "", f"> {DRAFT_LABEL}. Check every step before using it.", ""]
    for heading, items, bullet in sections:
        body += [f"## {heading}", "", *(f"{bullet} {_one_line(item)}" for item in items), ""]
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n" + "\n".join(body)


def draft_runbook(
    issue: Issue,
    evidence: Sequence[str],
    fix: Remediation | None,
    llm: StructuredLLM,
    redactor: Redactor,
) -> DraftResult:
    """Ask the model for a runbook for ``issue``, render it, and check it like our own."""
    meter = UsageMeter("assistant", MODEL)
    lines = "\n".join(_escape(redactor.redact(line)) for line in evidence[:MAX_EVIDENCE_LINES])
    tried = "\n".join(f"- {_escape(step)}" for step in fix.steps) if fix else "- none"
    user = (
        f"{TASK_RUNBOOK}<issue>\n{issue.id}: {_escape(issue.title)}\ncategory={issue.category} "
        f"severity={issue.severity} service={_escape(issue.affected_service or '-')}\n"
        f"steps suggested so far:\n{tried}\n</issue>\n<evidence>\n{lines}\n</evidence>"
    )
    started = time.perf_counter()
    try:
        reply = llm(MODEL, load_prompt("assistant"), user, RunbookFields)
    except LLMError as exc:
        meter.record_failure(exc, time.perf_counter() - started)
        return DraftResult(error=str(exc), usage=meter.usage)
    meter.record(reply)
    text = render_runbook(reply.parsed, issue)
    runbook = parse_runbook(text, "draft")
    categories = frozenset(category.value for category in Category)
    return DraftResult(
        text=text,
        runbook=runbook,
        problems=check_runbook(runbook, categories),
        usage=meter.usage,
    )


# ------------------------------------------------------------------ chat about the run

Tool = Literal["issues", "evidence", "fix", "checklist", "tickets", "costs"]


class ChatStep(BaseModel):
    """One turn: a tool to call, or the final answer."""

    tool: Tool | None = Field(default=None, description="A tool to call, or null to answer")
    issue_id: str | None = Field(default=None, description="For evidence and fix, e.g. ISS-1")
    answer: str | None = Field(default=None, description="The answer, when no tool is called")


@dataclass
class ChatResult:
    """The answer, the tools used, and what it cost."""

    answer: str = ""
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None
    usage: AgentUsage | None = None


class _ChatState(TypedDict):
    transcript: list[str]
    steps: int
    answer: str
    tools_used: list[str]
    error: str | None


def _issue_line(issue: Issue) -> str:
    lines = ", ".join(f"L{n}" for n in issue.evidence_lines[:20])
    return (
        f"{issue.id} [{issue.severity}] {_escape(issue.title)} "
        f"(service {_escape(issue.affected_service or '-')}, {issue.category}); evidence {lines}"
    )


def _fix_text(fix: Remediation | None) -> str:
    if fix is None:
        return "No fix was suggested for this issue."
    steps = "\n".join(f"{n}. {_escape(s)}" for n, s in enumerate(fix.steps, start=1))
    sources = ", ".join(f"{s.runbook} ({s.score:.2f})" for s in fix.sources) or "none"
    return (
        f"Root cause (hypothesis): {_escape(fix.root_cause)}\n{steps}\n"
        f"Why: {_escape(fix.rationale)}\nRunbooks: {sources}\nConfidence: {fix.confidence:.0%}"
    )


def _checklist_text(result: RunResult) -> str:
    groups = (
        ("Do now", result.checklist.do_now),
        ("This week", result.checklist.this_week),
        ("Prevent", result.checklist.prevent),
    )
    lines = [
        f"{name}: {_escape(item.text)} ({', '.join(item.issue_ids)})"
        for name, items in groups
        for item in items
    ]
    return "\n".join(lines) or "The checklist is empty."


def _tickets_text(result: RunResult) -> str:
    lines = [
        f"{t.issue_id}: {t.action}, key {t.key or t.related_key or '-'}, "
        f"{'sent' if t.sent else 'not sent'}" + (f", error: {_escape(t.error)}" if t.error else "")
        for t in result.tickets
    ]
    return "\n".join(lines) or "No tickets for this run."


def _costs_text(result: RunResult) -> str:
    lines = [
        f"{u.agent}: {u.input_tokens + u.output_tokens} tokens, ${u.cost_usd:.4f}"
        for u in result.usage
    ]
    return "\n".join([*lines, f"Total: ${result.total_cost_usd:.4f}"])


def run_tool(step: ChatStep, result: RunResult, evidence: dict[int, str]) -> str:
    """Run one read-only tool over this run's results. Unknown input gets a plain answer."""
    if step.tool == "issues":
        return "\n".join(_issue_line(i) for i in result.issues) or "No issues were found."
    if step.tool == "checklist":
        return _checklist_text(result)
    if step.tool == "tickets":
        return _tickets_text(result)
    if step.tool == "costs":
        return _costs_text(result)
    issue = next((i for i in result.issues if i.id == step.issue_id), None)
    if issue is None:
        return f"No issue {_escape(step.issue_id or '')!r}. Call issues to see the IDs."
    if step.tool == "fix":
        return _fix_text(next((f for f in result.remediations if f.issue_id == issue.id), None))
    return "\n".join(
        f"L{n}: {_escape(evidence[n][:MAX_CHARS_PER_MESSAGE])}"
        for n in issue.evidence_lines[:20]
        if n in evidence
    )


def ask_about_run(
    question: str,
    history: Sequence[tuple[str, str]],
    result: RunResult,
    evidence: dict[int, str],
    llm: StructuredLLM,
    token_budget: int,
) -> ChatResult:
    """Answer one question about this run, calling at most ``MAX_TOOL_CALLS`` tools."""
    spent = sum(u.input_tokens + u.output_tokens for u in result.usage)
    if spent >= token_budget:
        return ChatResult(error="This run has used its token budget.")
    meter = UsageMeter("assistant", MODEL)
    earlier = "\n".join(
        f"Q: {_escape(q)}\nA: {_escape(a)}" for q, a in list(history)[-MAX_HISTORY:]
    )
    header = (
        f'{TASK_CHAT}<run id="{result.run_id}" log="{_escape(result.log_name)}" />\n'
        + (f"<earlier>\n{earlier}\n</earlier>\n" if earlier else "")
        + f"<question>{_escape(question[:MAX_QUESTION_CHARS])}</question>"
    )

    def agent(state: _ChatState) -> dict[str, Any]:
        last_turn = state["steps"] >= MAX_TOOL_CALLS
        note = "\nYou have used all your tool calls: answer now." if last_turn else ""
        started = time.perf_counter()
        try:
            reply = llm(
                MODEL, load_prompt("assistant"), "\n".join(state["transcript"]) + note, ChatStep
            )
        except LLMError as exc:
            meter.record_failure(exc, time.perf_counter() - started)
            return {"error": str(exc)}
        meter.record(reply)
        step = reply.parsed
        if step.tool is None or last_turn:
            fallback = "I couldn't answer that within my tool limit. Try a narrower question."
            return {"answer": step.answer or fallback}
        call = f"{step.tool}({step.issue_id or ''})"
        output = run_tool(step, result, evidence)
        return {
            "transcript": [
                *state["transcript"],
                f'<tool_result call="{call}">\n{output}\n</tool_result>',
            ],
            "steps": state["steps"] + 1,
            "tools_used": [*state["tools_used"], call],
        }

    def route(state: _ChatState) -> str:
        return END if state["answer"] or state["error"] else "agent"

    graph = StateGraph(_ChatState)
    graph.add_node("agent", agent)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["agent", END])
    final = graph.compile().invoke(
        _ChatState(transcript=[header], steps=0, answer="", tools_used=[], error=None)
    )
    return ChatResult(
        answer=final["answer"],
        tools_used=final["tools_used"],
        error=final["error"],
        usage=meter.usage,
    )
