"""Cookbook Synthesizer agent (build plan section 3, step 4).

Turns the fixes into one checklist: do now, this week, prevent next time. Items may only point
at issues that exist, so every line of the checklist traces back to evidence.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from incident_suite.agents import LLMError, StructuredLLM
from incident_suite.prompts import load_prompt
from incident_suite.state import Checklist, ChecklistItem, Issue, Remediation, RunSettings
from incident_suite.telemetry import UsageMeter

MAX_ITEMS_PER_GROUP = 6
_TAG = re.compile(r"(?i)<\s*/?\s*issues\b[^>]*>")


class ItemDraft(BaseModel):
    """One checklist line as the model writes it."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=300)
    issue_ids: list[str]


class CookbookAnswer(BaseModel):
    """The model's checklist."""

    model_config = ConfigDict(extra="forbid")

    do_now: list[ItemDraft]
    this_week: list[ItemDraft]
    prevent: list[ItemDraft]


@dataclass
class CookbookResult:
    """The checklist, and any problem met making it."""

    checklist: Checklist
    errors: list[str] = field(default_factory=list)


def _escape(text: str) -> str:
    return _TAG.sub("[tag]", text)


def _user_message(issues: Sequence[Issue], fixes: dict[str, Remediation]) -> str:
    blocks = []
    for issue in issues:
        fix = fixes.get(issue.id)
        lines = [f"{issue.id} [{issue.severity}] {issue.title} ({issue.affected_service or '-'})"]
        if fix is not None:
            lines.append(f"  root cause: {fix.root_cause}")
            lines += [f"  step: {step}" for step in fix.steps]
        blocks.append(_escape("\n".join(lines)))
    return "<issues>\n" + "\n\n".join(blocks) + "\n</issues>"


def _items(drafts: Sequence[ItemDraft], known: set[str]) -> list[ChecklistItem]:
    items = []
    for draft in drafts[:MAX_ITEMS_PER_GROUP]:
        ids = [issue_id for issue_id in dict.fromkeys(draft.issue_ids) if issue_id in known]
        if ids:
            items.append(ChecklistItem(text=draft.text, issue_ids=ids))
    return items


def synthesize(
    issues: Sequence[Issue],
    remediations: Sequence[Remediation],
    settings: RunSettings,
    llm: StructuredLLM,
    meter: UsageMeter,
) -> CookbookResult:
    """Build the checklist from the fixes. No fixes means an empty checklist and no model call."""
    if not remediations:
        return CookbookResult(checklist=Checklist())
    fixes = {fix.issue_id: fix for fix in remediations}
    system = settings.cookbook.system_prompt or load_prompt("cookbook")
    started = time.perf_counter()
    try:
        reply = llm(settings.cookbook.model, system, _user_message(issues, fixes), CookbookAnswer)
    except LLMError as exc:
        meter.record_failure(exc, time.perf_counter() - started)
        return CookbookResult(checklist=Checklist(), errors=[f"Cookbook: {exc}"])
    meter.record(reply)
    known = {issue.id for issue in issues}
    answer = reply.parsed
    return CookbookResult(
        checklist=Checklist(
            do_now=_items(answer.do_now, known),
            this_week=_items(answer.this_week, known),
            prevent=_items(answer.prevent, known),
        )
    )
