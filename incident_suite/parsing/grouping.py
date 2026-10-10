"""Group lines by error signature and keep only what the model needs.

Numbers, IDs, IPs, hashes and ports are masked, so 412 lines of
``Connection refused to payments-db.internal:5432 (attempt 2/3)`` become one pattern. Only
WARN, ERROR and FATAL patterns go forward, with a few lines of context around each and any
deploy or config change, so the cause just before the errors isn't lost.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from incident_suite.state import GroupedLog, LogLevel, LogLine, Pattern

MAX_PATTERNS = 200
MAX_CONTEXT_LINES = 500
MAX_CHANGE_LINES = 50
MAX_SIGNATURE_CHARS = 300
_FIRST_EXAMPLES = 5
_LAST_EXAMPLES = 3
_KEPT_LEVELS = frozenset({LogLevel.WARN, LogLevel.ERROR, LogLevel.FATAL})
_LEVEL_RANK = {level: rank for rank, level in enumerate(LogLevel)}
_SENTINEL = "\x00"

_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"), "<ip>"),
    (re.compile(r"(?<=[A-Za-z0-9\]]):\d{2,5}\b"), ":<port>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?=[A-Za-z%]{1,3}\b)"), "<num>"),
    (re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w{5,}\b"), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<num>"),
    (re.compile(r"\s+"), " "),
)
_CHANGE = re.compile(
    r"(?i)\b(deploy(ed|ing|ment)?|roll(ing)?[ -]?out|roll(ed)?[ -]?back|release[ds]?|upgrade[ds]?"
    r"|migrat(e|ed|ion)|config(uration)? (loaded|changed|reload(ed)?|updated)|restart(ed|ing)?"
    r"|scal(ed|ing) (up|down|to)|feature flag)\b"
)


def mask(message: str, keep: str | None = None) -> str:
    """The signature of a message: variable parts replaced by placeholders.

    ``keep`` (an HTTP status, say) is left as is, so 502s and 504s stay separate patterns.
    """
    text = message
    if keep:
        text = re.sub(rf"\b{re.escape(keep)}\b", _SENTINEL, text)
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    if keep:
        text = text.replace(_SENTINEL, keep)
    return text.strip()[:MAX_SIGNATURE_CHARS]


def signature_hash(service: str | None, level: LogLevel, error_code: str | None, sig: str) -> str:
    """Short, stable hash of a pattern. JIRA labels use it (``sig-<hash>``) to spot repeats."""
    key = f"{service or ''}|{level.value}|{error_code or ''}|{sig}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


@dataclass
class _Group:
    signature: str
    level: LogLevel
    service: str | None
    error_code: str | None
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    first_lines: list[int] = field(default_factory=list)
    last_lines: deque[int] = field(default_factory=lambda: deque(maxlen=_LAST_EXAMPLES))

    def add(self, line: LogLine) -> None:
        self.count += 1
        if len(self.first_lines) < _FIRST_EXAMPLES:
            self.first_lines.append(line.line_no)
        else:
            self.last_lines.append(line.line_no)
        if line.timestamp is not None:
            if self.first_seen is None or line.timestamp < self.first_seen:
                self.first_seen = line.timestamp
            if self.last_seen is None or line.timestamp > self.last_seen:
                self.last_seen = line.timestamp

    def to_pattern(self) -> Pattern:
        return Pattern(
            signature=self.signature,
            signature_hash=signature_hash(
                self.service, self.level, self.error_code, self.signature
            ),
            level=self.level,
            count=self.count,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            example_line_nos=[*self.first_lines, *self.last_lines],
            service=self.service,
            error_code=self.error_code,
        )


def _context(lines: Sequence[LogLine], patterns: Sequence[Pattern], window: int) -> list[LogLine]:
    if window == 0:
        return []
    position = {line.line_no: index for index, line in enumerate(lines)}
    wanted: set[int] = set()
    for pattern in patterns:
        index = position[pattern.example_line_nos[0]]
        wanted.update(range(max(0, index - window), min(len(lines), index + window + 1)))
        if len(wanted) >= MAX_CONTEXT_LINES:
            break
    return [lines[index] for index in sorted(wanted)[:MAX_CONTEXT_LINES]]


def group_lines(lines: Sequence[LogLine], context_lines: int = 3) -> GroupedLog:
    """Group WARN+ lines into patterns, worst and most frequent first, with context."""
    groups: dict[tuple[str, LogLevel, str, str], _Group] = {}
    changes: list[LogLine] = []
    quiet = 0
    for line in lines:
        if line.level not in _KEPT_LEVELS:
            quiet += 1
            if len(changes) < MAX_CHANGE_LINES and _CHANGE.search(line.message):
                changes.append(line)
            continue
        sig = mask(line.message, keep=line.error_code)
        key = (line.service or "", line.level, line.error_code or "", sig)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _Group(sig, line.level, line.service, line.error_code)
        group.add(line)

    ordered = sorted(groups.values(), key=lambda g: (-_LEVEL_RANK[g.level], -g.count))
    patterns = [group.to_pattern() for group in ordered[:MAX_PATTERNS]]
    return GroupedLog(
        patterns=patterns,
        patterns_dropped=max(0, len(ordered) - MAX_PATTERNS),
        context_lines=_context(lines, patterns, context_lines),
        change_lines=changes,
        quiet_line_count=quiet,
    )
