"""Run IDs, and tokens, cost and time per agent (build plan sections 5 and 12.1).

The LangSmith trace link joins this module when tracing is wired up (H8).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from incident_suite.config import CHAT_MODELS
from incident_suite.llm import LLMError, LLMReply
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
