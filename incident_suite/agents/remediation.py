"""Remediation agent.

For each issue: search the runbooks, then ask the model for a root cause, fix steps, rationale and
the runbook sections it used. Sources and their scores come from the retriever, never from the
model, so a cited runbook always exists. If the best match is weak or the model isn't confident,
the result asks for more context; the graph then widens the evidence and tries once more.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from incident_suite.agents import LLMError, StructuredLLM
from incident_suite.prompts import load_prompt
from incident_suite.rag.retriever import RunbookHit, RunbookRetriever
from incident_suite.state import Issue, LogLine, Remediation, RunbookSource, RunSettings
from incident_suite.telemetry import UsageMeter

MIN_RUNBOOK_SCORE = 0.35
MAX_LINE_CHARS = 300
MAX_EVIDENCE_LINES = 40
_QUERY_LINES = 3
_TAG = re.compile(r"(?i)<\s*/?\s*(?:issue|evidence|runbooks)\b[^>]*>")


class RemediationAnswer(BaseModel):
    """The model's answer for one issue."""

    model_config = ConfigDict(extra="forbid")

    root_cause: str
    steps: list[str] = Field(min_length=1, max_length=8)
    rationale: str
    source_ids: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class RemediationResult:
    """A fix (or ``None`` if the call failed), and why more context might help."""

    remediation: Remediation | None
    retry_reasons: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def needs_more_context(self) -> bool:
        """True when the issue should go back to the Log Reader for a wider window."""
        return self.remediation is not None and bool(self.retry_reasons)


def _escape(text: str) -> str:
    """Stop log or runbook text from opening or closing the prompt's tags."""
    return _TAG.sub("[tag]", text)


def search_query(issue: Issue, evidence: Sequence[LogLine]) -> str:
    """What to search the runbooks with: the issue plus a few evidence messages."""
    messages = [line.message for line in evidence[:_QUERY_LINES]]
    return "\n".join([issue.title, issue.category.value.replace("_", " "), *messages])


def _user_message(issue: Issue, evidence: Sequence[LogLine], hits: Sequence[RunbookHit]) -> str:
    lines = "\n".join(
        f"L{line.line_no}: {_escape(line.raw[:MAX_LINE_CHARS])}"
        for line in evidence[:MAX_EVIDENCE_LINES]
    )
    runbooks = "\n\n".join(
        f"[{hit.id}] {hit.title} (score {hit.score:.2f})\n{_escape(hit.text)}" for hit in hits
    )
    return (
        f"<issue>\n{issue.id}: {_escape(issue.title)}\ncategory={issue.category} "
        f"severity={issue.severity} service={_escape(issue.affected_service or '-')}\n</issue>\n"
        f"<evidence>\n{lines}\n</evidence>\n"
        f"<runbooks>\n{runbooks or 'No runbook matched.'}\n</runbooks>"
    )


def _sources(answer: RemediationAnswer, hits: Sequence[RunbookHit]) -> list[RunbookSource]:
    by_id = {hit.id: hit for hit in hits}
    return [
        RunbookSource(runbook=hit.runbook, score=hit.score)
        for hit in (by_id[sid] for sid in dict.fromkeys(answer.source_ids) if sid in by_id)
    ]


def _retry_reasons(
    answer: RemediationAnswer, hits: Sequence[RunbookHit], threshold: float
) -> list[str]:
    reasons = []
    best = max((hit.score for hit in hits), default=0.0)
    if not hits:
        reasons.append("no runbook matched")
    elif best < MIN_RUNBOOK_SCORE:
        reasons.append(f"best runbook match {best:.2f} is below {MIN_RUNBOOK_SCORE:.2f}")
    if answer.confidence < threshold:
        reasons.append(f"model confidence {answer.confidence:.2f} is below {threshold:.2f}")
    return reasons


def remediate(
    issue: Issue,
    evidence: Sequence[LogLine],
    *,
    retriever: RunbookRetriever,
    settings: RunSettings,
    llm: StructuredLLM,
    meter: UsageMeter,
) -> RemediationResult:
    """Find a fix for one issue, citing the runbook sections it came from."""
    hits = retriever.search(search_query(issue, evidence), settings.remediation.top_k)
    system = settings.remediation.system_prompt or load_prompt("remediation")
    started = time.perf_counter()
    try:
        reply = llm(
            settings.remediation.model,
            system,
            _user_message(issue, evidence, hits),
            RemediationAnswer,
        )
    except LLMError as exc:
        meter.record_failure(exc, time.perf_counter() - started)
        return RemediationResult(remediation=None, error=f"Remediation ({issue.id}): {exc}")
    meter.record(reply)
    answer = reply.parsed
    remediation = Remediation(
        issue_id=issue.id,
        root_cause=answer.root_cause,
        steps=answer.steps,
        rationale=answer.rationale,
        sources=_sources(answer, hits),
        confidence=answer.confidence,
    )
    return RemediationResult(
        remediation=remediation,
        retry_reasons=_retry_reasons(answer, hits, settings.remediation.confidence_threshold),
    )
