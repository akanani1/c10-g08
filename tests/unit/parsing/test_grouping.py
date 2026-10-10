"""Tests for incident_suite/parsing/grouping.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from incident_suite.parsing.grouping import (
    MAX_PATTERNS,
    group_lines,
    mask,
    signature_hash,
)
from incident_suite.state import LogLevel, LogLine

START = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)


def _line(
    line_no: int,
    message: str,
    level: LogLevel = LogLevel.ERROR,
    *,
    service: str | None = "api",
    error_code: str | None = None,
    seconds: int | None = None,
) -> LogLine:
    return LogLine(
        line_no=line_no,
        raw=message,
        message=message,
        level=level,
        service=service,
        error_code=error_code,
        timestamp=None if seconds is None else START + timedelta(seconds=seconds),
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Connection refused to db.internal:5432 (attempt 2/3)",
            "Connection refused to db.internal:<port> (attempt <num>/<num>)",
        ),
        ("from 10.20.1.16:443 and 10.0.0.1", "from <ip> and <ip>"),
        ("id 123e4567-e89b-12d3-a456-426614174000 done", "id <uuid> done"),
        ("trace 7f3a01da9c and 0xDEADBEEF", "trace <hex> and <hex>"),
        ("took 1602ms, used 612Mi", "took <num>ms, used <num>Mi"),
        ('pod="shop/orders-6d9f7c-xk2lp"', 'pod="shop/orders-<id>-<id>"'),
        ("  many    spaces ", "many spaces"),
        ("[REDACTED:email] failed", "[REDACTED:email] failed"),
    ],
)
def test_mask(message: str, expected: str) -> None:
    assert mask(message) == expected


def test_mask_keeps_the_status_code() -> None:
    assert mask("POST /api/checkout 502 in 30ms", keep="502") == "POST /api/checkout 502 in <num>ms"


def test_signature_hash_is_stable_and_short() -> None:
    first = signature_hash("api", LogLevel.ERROR, None, "boom <num>")
    assert first == signature_hash("api", LogLevel.ERROR, None, "boom <num>")
    assert first != signature_hash("web", LogLevel.ERROR, None, "boom <num>")
    assert len(first) == 12


def test_grouping_counts_times_and_examples() -> None:
    lines = [
        _line(i, f"Connection refused to db:5432 attempt {i}", seconds=10 - i) for i in range(1, 11)
    ]
    grouped = group_lines(lines)
    (pattern,) = grouped.patterns
    assert pattern.count == 10
    assert pattern.first_seen == START
    assert pattern.last_seen == START + timedelta(seconds=9)
    assert pattern.example_line_nos == [1, 2, 3, 4, 5, 8, 9, 10]
    assert pattern.service == "api"


def test_quiet_lines_are_not_patterns_and_changes_are_kept() -> None:
    lines = [
        _line(1, "Deploy started: api v2.15.0", LogLevel.INFO),
        _line(2, "GET /health 200", LogLevel.INFO),
        _line(3, "Config loaded: pool=5", LogLevel.UNKNOWN),
        _line(4, "Connection refused", LogLevel.ERROR),
    ]
    grouped = group_lines(lines, context_lines=0)
    assert [p.signature for p in grouped.patterns] == ["Connection refused"]
    assert [line.line_no for line in grouped.change_lines] == [1, 3]
    assert grouped.quiet_line_count == 3
    assert grouped.context_lines == []


def test_patterns_sorted_by_level_then_count() -> None:
    lines = [
        _line(1, "slow query", LogLevel.WARN),
        _line(2, "slow query", LogLevel.WARN),
        _line(3, "disk full", LogLevel.ERROR),
        _line(4, "process died", LogLevel.FATAL),
    ]
    assert [p.level for p in group_lines(lines).patterns] == [
        LogLevel.FATAL,
        LogLevel.ERROR,
        LogLevel.WARN,
    ]


def test_status_codes_and_services_stay_separate() -> None:
    lines = [
        _line(1, "POST /x 502", error_code="502"),
        _line(2, "POST /x 504", error_code="504"),
        _line(3, "POST /x 502", error_code="502", service="other"),
    ]
    assert len(group_lines(lines).patterns) == 3


def test_context_window_around_first_occurrence() -> None:
    lines = [_line(i, f"info {i}", LogLevel.INFO) for i in range(1, 11)]
    lines[5] = _line(6, "boom")
    grouped = group_lines(lines, context_lines=2)
    assert [line.line_no for line in grouped.context_lines] == [4, 5, 6, 7, 8]


def test_pattern_cap() -> None:
    lines = [_line(i, f"error kind {chr(65 + i % 26)}{chr(65 + i // 26)}") for i in range(1, 251)]
    grouped = group_lines(lines, context_lines=1)
    assert len(grouped.patterns) == MAX_PATTERNS
    assert grouped.patterns_dropped == 50
