"""Assistant: what has happened in Slack and JIRA since this run posted (plan section 17.2).

A small, read-only LangGraph agent with three steps:

    read_slack -> read_jira -> summarise

- **read_slack** reads the replies in this run's Slack thread. For a ticket that was seen
  before, it also reads the original incident's thread, which we stored on the ticket as the
  JIRA issue property ``incident_suite.slack`` when we first posted.
- **read_jira** reads status, assignee and the latest comments of this run's tickets. Which
  tickets, and which fields, is fixed in code: the model never writes a query.
- **summarise** asks the cheap model for a short summary. Everything it reads is redacted and
  wrapped in tags, as data and never as instructions.

The readers are protocols; the Slack and JIRA clients implement them, and tests use fakes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from incident_suite.agents import LLMError, StructuredLLM
from incident_suite.parsing.redact import Redactor
from incident_suite.prompts import load_prompt
from incident_suite.state import AgentUsage, RunResult, TicketAction
from incident_suite.telemetry import UsageMeter

MODEL = "openai/gpt-5-nano"  # the cheap model on the allow-list
MAX_CHARS_PER_MESSAGE = 1_000
_TAG = re.compile(r"</?\s*(slack_thread|jira_ticket|run)\b[^>]*>", re.IGNORECASE)


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

    result: RunResult
    threads: ThreadReader | None
    tickets: TicketReader | None
    llm: StructuredLLM
    redactor: Redactor
    token_budget: int


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
    """``(label, channel, ts)`` for this run's thread and each seen-before ticket's thread."""
    result = data.result
    threads = []
    if result.slack.channel and result.slack.ts:
        threads.append(("this run", result.slack.channel, result.slack.ts))
    if data.tickets is not None:
        for ticket in result.tickets:
            if ticket.action is TicketAction.COMMENT and ticket.key:
                try:
                    stored = data.tickets.slack_thread(ticket.key)
                except ReaderError:
                    continue
                if stored is not None and stored[1] not in {t[2] for t in threads}:
                    threads.append((f"first seen in {ticket.key}", *stored))
    return threads


def _graph(data: AssistantInput) -> Any:
    def read_slack(state: _State) -> dict[str, Any]:
        reader = data.threads
        if reader is None:
            return {}
        blocks, sources, problems = list(state["blocks"]), list(state["sources"]), []
        for label, channel, ts in _threads_to_read(data):
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
        for key in (t.key for t in data.result.tickets if t.sent and t.key):
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
        user = (
            f'<run id="{data.result.run_id}" log="{_escape(data.result.log_name)}">\n'
            + "\n\n".join(state["blocks"])
            + "\n</run>"
        )
        started = time.perf_counter()
        try:
            reply = data.llm(MODEL, load_prompt("assistant"), user, AssistantAnswer)
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
    """Read this run's Slack threads and tickets, and summarise what changed."""
    spent = sum(u.input_tokens + u.output_tokens for u in data.result.usage)
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
