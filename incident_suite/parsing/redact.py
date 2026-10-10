"""Mask secrets and personal data before any model, screen, Slack or JIRA sees them (section 18.2).

Regex only, so it is cheap even on the 50k-line log. Each match becomes ``[REDACTED:<type>]``.
It is pattern-based and can miss formats it doesn't know; the README says so.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from incident_suite.state import RedactionType

# Key names whose values are secrets (names only, no secret values)
_SECRET_KEY_NAMES = (
    "password", "passwd", "pwd", "pass", "secret", r"client[_-]?secret", "token",
    r"access[_-]?token", r"refresh[_-]?token", r"api[_-]?key", "apikey", r"access[_-]?key",
    r"secret[_-]?key", r"private[_-]?key", "auth", "authorization", "credentials?",
    r"session[_-]?id", "cookie",
)  # fmt: skip
_SECRET_KEYS = "|".join(_SECRET_KEY_NAMES)

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")

# Order matters: JWTs before bearer tokens (so "Bearer <jwt>" counts as a JWT), and the
# key/value rule after the specific ones, so it doesn't swallow them.
_RULES: tuple[tuple[RedactionType, re.Pattern[str], str], ...] = (
    (
        RedactionType.JWT,
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        "[REDACTED:jwt]",
    ),
    (
        RedactionType.BEARER_TOKEN,
        re.compile(r"(?i)\b(bearer)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1 [REDACTED:bearer_token]",
    ),
    (
        RedactionType.AWS_ACCESS_KEY,
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
        "[REDACTED:aws_access_key]",
    ),
    (
        RedactionType.CREDENTIAL_PAIR,
        re.compile(
            rf"""(?ix)
            (?<![\w-])
            (["']?(?:[\w-]*[_-])?(?:{_SECRET_KEYS})["']?)   # key, maybe quoted or prefixed
            (\s*[=:]\s*)                                     # = or :
            (?!\[REDACTED|bearer\s)
            ("[^"]*"|'[^']*'|[^\s,;&"'}}]+)                  # value
            """
        ),
        r"\1\2[REDACTED:credential_pair]",
    ),
    (
        RedactionType.EMAIL,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED:email]",
    ),
)

_SECRET_COLUMN = re.compile(rf"(?i)^(?:[\w.-]*[._-])?(?:{_SECRET_KEYS})$")


class Redactor:
    """Applies the enabled rules and counts what it masked, per type."""

    def __init__(self, enabled: Iterable[RedactionType] = tuple(RedactionType)) -> None:
        self.enabled = frozenset(enabled)
        self.counts: Counter[RedactionType] = Counter()
        self._in_key_block = False

    def redact(self, text: str) -> str:
        """Redact one string (a line, a cell, or a whole message)."""
        if RedactionType.PRIVATE_KEY in self.enabled:
            text, found = _PRIVATE_KEY_BLOCK.subn("[REDACTED:private_key]", text)
            self.counts[RedactionType.PRIVATE_KEY] += found
        for kind, pattern, replacement in _RULES:
            if kind in self.enabled:
                text, found = pattern.subn(replacement, text)
                self.counts[kind] += found
        return text

    def redact_line(self, line: str) -> str:
        """Redact one line of a text log, masking every line of a multi-line private key."""
        if RedactionType.PRIVATE_KEY in self.enabled:
            if self._in_key_block:
                if _PRIVATE_KEY_END.search(line):
                    self._in_key_block = False
                return "[REDACTED:private_key]"
            begin = _PRIVATE_KEY_BEGIN.search(line)
            if begin and not _PRIVATE_KEY_END.search(line):
                self._in_key_block = True
                self.counts[RedactionType.PRIVATE_KEY] += 1
                return self.redact(line[: begin.start()]) + "[REDACTED:private_key]"
        return self.redact(line)

    def redact_field(self, name: str, value: str) -> str:
        """Redact a named field. A column called ``password`` or ``api_key`` is masked whole."""
        if (
            RedactionType.CREDENTIAL_PAIR in self.enabled
            and value
            and not value.startswith("[REDACTED")
            and _SECRET_COLUMN.match(name)
        ):
            self.counts[RedactionType.CREDENTIAL_PAIR] += 1
            return "[REDACTED:credential_pair]"
        return self.redact(value)

    def summary(self) -> dict[RedactionType, int]:
        """Counts per type, leaving out types with no matches."""
        return {kind: count for kind, count in self.counts.items() if count}
