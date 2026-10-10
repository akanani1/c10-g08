"""Run IDs, tokens, cost and time per agent, and the LangSmith trace link.

Tracing is optional. When ``LANGSMITH_TRACING`` is on and a key is set, :func:`enable_tracing`
switches LangChain's tracer on for this process, and :func:`trace_link` gives the URL of a
run's trace before the run starts, because we choose the trace's ID ourselves. That link goes
into the app, the Slack summary and the JIRA tickets. If LangSmith can't be reached, the run
carries on without a link.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, MutableMapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langsmith import Client
from langsmith.utils import LangSmithError

from incident_suite.agents import LLMError, LLMReply
from incident_suite.config import CHAT_MODELS, AppConfig
from incident_suite.state import AgentUsage

_PER_MILLION = 1_000_000


def new_run_id(now: datetime | None = None) -> str:
    """A readable, unique run ID, e.g. ``run-20261010-141502-3fa9c1``."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(3)}"


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of a call at the list price in ``config.CHAT_MODELS``."""
    price = CHAT_MODELS[model]
    return (
        input_tokens * price.input_per_million + output_tokens * price.output_per_million
    ) / _PER_MILLION


class UsageMeter:
    """Adds up the calls one agent makes during a run."""

    def __init__(self, agent: str, model: str) -> None:
        self._model = model
        self._usage = AgentUsage(agent=agent, model=model)

    def record(self, reply: LLMReply[Any]) -> None:
        """Count a successful call."""
        self._add(reply.input_tokens, reply.output_tokens, reply.seconds)

    def record_failure(self, error: LLMError, seconds: float) -> None:
        """Count the tokens a failed call had already spent."""
        self._add(error.input_tokens, error.output_tokens, seconds)

    def _add(self, input_tokens: int, output_tokens: int, seconds: float) -> None:
        usage = self._usage
        self._usage = usage.model_copy(
            update={
                "calls": usage.calls + 1,
                "input_tokens": usage.input_tokens + input_tokens,
                "output_tokens": usage.output_tokens + output_tokens,
                "seconds": usage.seconds + seconds,
                "cost_usd": usage.cost_usd + cost_usd(self._model, input_tokens, output_tokens),
            }
        )

    @property
    def tokens(self) -> int:
        """Input plus output tokens so far."""
        return self._usage.input_tokens + self._usage.output_tokens

    @property
    def usage(self) -> AgentUsage:
        """The totals so far."""
        return self._usage


def merge_usage(entries: Sequence[AgentUsage]) -> list[AgentUsage]:
    """One row per agent, adding up repeated calls (Remediation runs again on a retry)."""
    merged: dict[str, AgentUsage] = {}
    for entry in entries:
        current = merged.get(entry.agent)
        if current is None:
            merged[entry.agent] = entry
            continue
        merged[entry.agent] = current.model_copy(
            update={
                "calls": current.calls + entry.calls,
                "input_tokens": current.input_tokens + entry.input_tokens,
                "output_tokens": current.output_tokens + entry.output_tokens,
                "cost_usd": current.cost_usd + entry.cost_usd,
                "seconds": current.seconds + entry.seconds,
            }
        )
    return list(merged.values())


def enable_tracing(config: AppConfig, environ: MutableMapping[str, str] = os.environ) -> bool:
    """Turn on LangChain's LangSmith tracer for this process when the config asks for it.

    LangChain reads these from the environment; ``environ`` lets tests pass their own.
    """
    if not config.langsmith_tracing or config.langsmith_api_key is None:
        return False
    environ["LANGSMITH_TRACING"] = "true"
    environ["LANGSMITH_API_KEY"] = config.langsmith_api_key.get_secret_value()
    environ["LANGSMITH_PROJECT"] = config.langsmith_project
    return True


def trace_link(
    config: AppConfig, trace_id: UUID, client_factory: Callable[..., Any] = Client
) -> str | None:
    """The LangSmith URL the run with ``trace_id`` will have, or ``None`` if there's none.

    The project is created if it doesn't exist yet, so the link works for the very first run.
    """
    if config.langsmith_api_key is None:
        return None
    try:
        client = client_factory(api_key=config.langsmith_api_key.get_secret_value())
        project = client.create_project(config.langsmith_project, upsert=True)
    except LangSmithError:
        return None
    return f"{project.url}/r/{trace_id}?poll=true" if project.url else None
