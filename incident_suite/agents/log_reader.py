"""Log Reader / Classifier agent (build plan section 3, step 2).

Turns the pre-filtered patterns into classified issues. The model sees a compact digest wrapped
in ``<log>`` delimiters, never the raw upload, and big logs are split into chunks that are
classified separately and merged. Evidence lines are checked against what the model was shown,
so every issue points at real lines.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from incident_suite.llm import LLMError, StructuredLLM
from incident_suite.prompts import load_prompt
from incident_suite.state import (
    Category,
    GroupedLog,
    Issue,
    LogLine,
    ParsedLog,
    Pattern,
    RunSettings,
    Severity,
)
from incident_suite.telemetry import UsageMeter

MAX_DIGEST_CHARS = 24_000  # per chunk, roughly 6k tokens
MAX_EXAMPLES = 3
MAX_EXAMPLE_CHARS = 300
MAX_CHANGES = 20
MAX_EVIDENCE = 10
MAX_WIDE_CONTEXT = 60
_TAG = re.compile(r"(?i)<\s*/?\s*log\b[^>]*>")


class IssueDraft(BaseModel):
    """One issue as the model reports it, before IDs and evidence are checked."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    pattern_ids: list[str]
    evidence_lines: list[int]
    affected_service: str | None


class ClassifierAnswer(BaseModel):
    """The model's whole answer for one chunk."""

    model_config = ConfigDict(extra="forbid")

    issues: list[IssueDraft]


@dataclass
class ClassifierResult:
    """Issues with IDs (``ISS-1`` ...) and any problems met along the way."""

    issues: list[Issue]
    errors: list[str] = field(default_factory=list)


@dataclass
class _Merged:
    draft: IssueDraft
    patterns: list[Pattern]
    evidence: list[int]


def _defuse(text: str) -> str:
    """Stop log text from closing or opening the ``<log>`` delimiter."""
    return _TAG.sub("[log-tag]", text)


def _line_text(line: LogLine) -> str:
    return _defuse(line.raw[:MAX_EXAMPLE_CHARS])


def _render_pattern(pid: str, pattern: Pattern, by_number: dict[int, LogLine]) -> str:
    first = pattern.first_seen.isoformat() if pattern.first_seen else "-"
    last = pattern.last_seen.isoformat() if pattern.last_seen else "-"
    head = (
        f"[{pid}] {pattern.level} x{pattern.count} service={pattern.service or '-'} "
        f"code={pattern.error_code or '-'} first={first} last={last}\n"
        f"  signature: {_defuse(pattern.signature)}"
    )
    examples = [
        f"  L{number}: {_line_text(by_number[number])}"
        for number in pattern.example_line_nos[:MAX_EXAMPLES]
        if number in by_number
    ]
    return "\n".join([head, *examples])


def _chunks(blocks: Sequence[str]) -> list[list[int]]:
    """Split pattern blocks into chunks under the size limit, keeping their order."""
    chunks: list[list[int]] = [[]]
    size = 0
    for index, block in enumerate(blocks):
        if chunks[-1] and size + len(block) > MAX_DIGEST_CHARS:
            chunks.append([])
            size = 0
        chunks[-1].append(index)
        size += len(block)
    return chunks


def _digest(
    parsed: ParsedLog,
    blocks: Sequence[str],
    chunk_patterns: Sequence[Pattern],
    grouped: GroupedLog,
    window: int,
) -> tuple[str, set[int]]:
    """The user message for one chunk, and every line number it shows the model."""
    anchors = [p.example_line_nos[0] for p in chunk_patterns]
    changes = grouped.change_lines[:MAX_CHANGES]
    listed = {line.line_no for line in changes}
    context = [
        line
        for line in grouped.context_lines
        if line.line_no not in listed
        and any(abs(line.line_no - anchor) <= window for anchor in anchors)
    ]
    shown = {n for p in chunk_patterns for n in p.example_line_nos[:MAX_EXAMPLES]}
    shown |= {line.line_no for line in context + changes}
    parts = [
        f'<log name="{_defuse(parsed.name)}" format="{parsed.format}" lines="{len(parsed.lines)}">',
        "PATTERNS:",
        *blocks,
        "CHANGES:",
        *(f"L{line.line_no}: {_line_text(line)}" for line in changes),
        "CONTEXT:",
        *(f"L{line.line_no}: {_line_text(line)}" for line in context),
        "</log>",
    ]
    return "\n".join(parts), shown


def _check_draft(draft: IssueDraft, ids: dict[str, Pattern], shown: set[int]) -> _Merged | None:
    patterns = [ids[pid] for pid in dict.fromkeys(draft.pattern_ids) if pid in ids]
    if not patterns:
        return None
    allowed = shown | {n for p in patterns for n in p.example_line_nos}
    evidence = [n for n in dict.fromkeys(draft.evidence_lines) if n in allowed]
    if not evidence:
        evidence = [n for p in patterns for n in p.example_line_nos]
    return _Merged(draft, patterns, evidence[:MAX_EVIDENCE])


def _merge(found: list[_Merged]) -> list[_Merged]:
    """Merge issues from different chunks that share a category and service."""
    merged: dict[tuple[Category, str], _Merged] = {}
    for item in found:
        key = (item.draft.category, (item.draft.affected_service or "").lower())
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        worse = item if item.draft.severity.rank > existing.draft.severity.rank else existing
        merged[key] = _Merged(
            draft=worse.draft.model_copy(
                update={"confidence": max(item.draft.confidence, existing.draft.confidence)}
            ),
            patterns=existing.patterns + item.patterns,
            evidence=sorted(set(existing.evidence + item.evidence))[:MAX_EVIDENCE],
        )
    return list(merged.values())


def _to_issue(number: int, item: _Merged) -> Issue:
    lead = max(item.patterns, key=lambda p: p.count)
    return Issue(
        id=f"ISS-{number}",
        title=item.draft.title,
        category=item.draft.category,
        severity=item.draft.severity,
        confidence=item.draft.confidence,
        evidence_lines=sorted(item.evidence),
        affected_service=item.draft.affected_service or lead.service,
        signature_hash=lead.signature_hash,
    )


def classify(
    parsed: ParsedLog,
    grouped: GroupedLog,
    settings: RunSettings,
    llm: StructuredLLM,
    meter: UsageMeter,
) -> ClassifierResult:
    """Classify the WARN+ patterns of one log into issues, worst first."""
    if not grouped.patterns:
        return ClassifierResult(issues=[])
    by_number = {line.line_no: line for line in parsed.lines}
    pids = [f"P{index}" for index in range(1, len(grouped.patterns) + 1)]
    blocks = [
        _render_pattern(pid, pattern, by_number)
        for pid, pattern in zip(pids, grouped.patterns, strict=True)
    ]
    system = settings.log_reader.system_prompt or load_prompt("log_reader")
    found: list[_Merged] = []
    errors: list[str] = []
    chunks = _chunks(blocks)
    for done, chunk in enumerate(chunks):
        if meter.tokens >= settings.token_budget:
            skipped = sum(len(rest) for rest in chunks[done:])
            errors.append(f"Token budget reached; {skipped} pattern groups were not classified.")
            break
        ids = {pids[i]: grouped.patterns[i] for i in chunk}
        user, shown = _digest(
            parsed,
            [blocks[i] for i in chunk],
            list(ids.values()),
            grouped,
            settings.log_reader.context_lines,
        )
        started = time.perf_counter()
        try:
            reply = llm(settings.log_reader.model, system, user, ClassifierAnswer)
        except LLMError as exc:
            meter.record_failure(exc, time.perf_counter() - started)
            errors.append(f"Log Reader: {exc}")
            continue
        meter.record(reply)
        checked = [_check_draft(draft, ids, shown) for draft in reply.parsed.issues]
        found += [item for item in checked if item is not None]

    ordered = sorted(
        _merge(found),
        key=lambda m: (-m.draft.severity.rank, -sum(p.count for p in m.patterns)),
    )
    issues = [_to_issue(number, item) for number, item in enumerate(ordered, start=1)]
    return ClassifierResult(issues=issues, errors=errors)


def widen_context(parsed: ParsedLog, issue: Issue, window: int) -> list[LogLine]:
    """Lines around an issue's evidence, for Remediation's "need more context" retry."""
    index = {line.line_no: position for position, line in enumerate(parsed.lines)}
    wanted: set[int] = set()
    for number in issue.evidence_lines:
        if number in index:
            centre = index[number]
            wanted.update(
                range(max(0, centre - window), min(len(parsed.lines), centre + window + 1))
            )
    return [parsed.lines[i] for i in sorted(wanted)][:MAX_WIDE_CONTEXT]
