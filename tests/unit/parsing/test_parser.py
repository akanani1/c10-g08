"""Tests for incident_suite/parsing/parser.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from incident_suite.parsing.parser import (
    ColumnMapper,
    detect_text_format,
    infer_level,
    level_from_status,
    normalize_level,
    parse_record,
    parse_text_line,
    parse_timestamp,
)
from incident_suite.state import LogFormat, LogLevel

YEAR = 2026
APP = "2026-10-09T14:01:14.003Z ERROR payment-api [trace=7f3a01da] Connection refused to db:5432"
NGINX = (
    '10.20.1.16 - - [09/Oct/2026:14:20:05 +0000] "POST /api/checkout?x=1 HTTP/1.1" 502 157 '
    '"-" "Mozilla/5.0" rt=0.004'
)
NGINX_ERR = (
    "2026/10/09 14:20:05 [error] 31#31: *4 upstream timed out (110: Connection timed out), "
    'client: 10.0.0.1, server: shop, request: "GET /search HTTP/1.1"'
)
SYSLOG = "Oct  9 14:12:09 node-7 kernel: [812345.112233] Memory cgroup out of memory: Killed"
KUBELET = (
    'Oct  9 14:12:09 node-7 kubelet[1123]: E1009 14:12:09.551002 pod="shop/orders-x" '
    'container="orders" reason=OOMKilled'
)


# ------------------------------------------------------------------ levels and times


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("WARNING", LogLevel.WARN),
        ("[err]", LogLevel.ERROR),
        ("Critical", LogLevel.FATAL),
        ("notice", LogLevel.INFO),
        ("50", LogLevel.ERROR),
        ("31", None),
        ("banana", None),
        (None, None),
    ],
)
def test_normalize_level(value: str | None, expected: LogLevel | None) -> None:
    assert normalize_level(value) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("goroutine panic: nil map", LogLevel.FATAL),
        ("Connection refused", LogLevel.ERROR),
        ("No space left on device", LogLevel.ERROR),
        ("retrying in 2s", LogLevel.WARN),
        ("user logged in", LogLevel.UNKNOWN),
    ],
)
def test_infer_level(message: str, expected: LogLevel) -> None:
    assert infer_level(message) is expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [("503", LogLevel.ERROR), ("429", LogLevel.WARN), ("200", LogLevel.INFO), ("DB_DOWN", None)],
)
def test_level_from_status(code: str, expected: LogLevel | None) -> None:
    assert level_from_status(code) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-09T14:01:14.003Z", datetime(2026, 10, 9, 14, 1, 14, 3000, tzinfo=UTC)),
        ("2026-10-09 16:01:14,500+02:00", datetime(2026, 10, 9, 14, 1, 14, 500000, tzinfo=UTC)),
        ("2026-10-09T14:01:14", datetime(2026, 10, 9, 14, 1, 14, tzinfo=UTC)),
        ("09/Oct/2026:14:20:05 +0000", datetime(2026, 10, 9, 14, 20, 5, tzinfo=UTC)),
        ("2026/10/09 14:20:05", datetime(2026, 10, 9, 14, 20, 5, tzinfo=UTC)),
        ("Oct  9 14:12:09", datetime(2026, 10, 9, 14, 12, 9, tzinfo=UTC)),
        ("1791640800", datetime(2026, 10, 10, 14, 0, tzinfo=UTC)),
        ("1791640800000", datetime(2026, 10, 10, 14, 0, tzinfo=UTC)),
        ("1791640800000000", datetime(2026, 10, 10, 14, 0, tzinfo=UTC)),
        ("1791640800000000000", datetime(2026, 10, 10, 14, 0, tzinfo=UTC)),
    ],
)
def test_parse_timestamp(text: str, expected: datetime) -> None:
    assert parse_timestamp(text, YEAR) == expected


@pytest.mark.parametrize("text", [None, "", "yesterday", "Foo 9 14:12:09", "Feb 31 10:00:00", "0"])
def test_unparseable_timestamps(text: str | None) -> None:
    assert parse_timestamp(text, YEAR) is None


# ------------------------------------------------------------------ text lines


def test_app_line() -> None:
    line = parse_text_line(7, APP, LogFormat.PLAIN, YEAR)
    assert line.line_no == 7
    assert line.level is LogLevel.ERROR
    assert line.service == "payment-api"
    assert line.trace_id == "7f3a01da"
    assert line.message == "Connection refused to db:5432"
    assert line.timestamp == datetime(2026, 10, 9, 14, 1, 14, 3000, tzinfo=UTC)


def test_app_line_key_values_and_thread() -> None:
    text = "2026-10-09T14:01:15Z ERROR [main] POST /v1/charges 503 error_code=DB_DOWN trace_id=-"
    line = parse_text_line(1, text, LogFormat.PLAIN, YEAR)
    assert line.error_code == "DB_DOWN"
    assert line.trace_id is None
    assert line.extra == {"thread": "main"}
    assert line.service is None


def test_app_line_with_dash_trace() -> None:
    line = parse_text_line(
        1, "2026-10-09T14:00:09Z INFO deployer [trace=-] Deploy", LogFormat.PLAIN, YEAR
    )
    assert line.trace_id is None
    assert line.service == "deployer"


def test_nginx_access_line() -> None:
    line = parse_text_line(3, NGINX, LogFormat.NGINX_ACCESS, YEAR)
    assert line.message == "POST /api/checkout 502"
    assert line.error_code == "502"
    assert line.level is LogLevel.ERROR
    assert line.extra["client_ip"] == "10.20.1.16"
    assert line.extra["request_time"] == "0.004"
    assert line.extra["user_agent"] == "Mozilla/5.0"


def test_nginx_error_line() -> None:
    line = parse_text_line(1, NGINX_ERR, LogFormat.NGINX_ERROR, YEAR)
    assert line.level is LogLevel.ERROR
    assert line.message == "upstream timed out (110: Connection timed out)"
    assert line.extra["context"].startswith("client: 10.0.0.1")


def test_syslog_kernel_line_infers_error() -> None:
    line = parse_text_line(5, SYSLOG, LogFormat.SYSLOG, YEAR)
    assert line.host == "node-7"
    assert line.service == "kernel"
    assert line.message == "Memory cgroup out of memory: Killed"
    assert line.level is LogLevel.ERROR


def test_klog_level_and_pod() -> None:
    line = parse_text_line(6, KUBELET, LogFormat.KUBERNETES, YEAR)
    assert line.level is LogLevel.ERROR
    assert line.service == "kubelet"
    assert line.extra == {"pod": "shop/orders-x", "container": "orders"}


def test_syslog_leading_level_word() -> None:
    line = parse_text_line(
        3, "Oct  9 14:11:30 node-7 orders[4410]: WARN cache full", LogFormat.SYSLOG, 2026
    )
    assert line.level is LogLevel.WARN
    assert line.message == "cache full"


def test_cri_line() -> None:
    text = "2026-10-09T14:00:00Z stderr F panic: runtime error"
    line = parse_text_line(1, text, LogFormat.KUBERNETES, YEAR)
    assert line.level is LogLevel.FATAL
    assert line.message == "panic: runtime error"


def test_cri_line_wrapping_an_app_line() -> None:
    text = "2026-10-09T14:00:00Z stdout F " + APP
    assert parse_text_line(1, text, LogFormat.PLAIN, YEAR).service == "payment-api"


def test_unknown_line_is_kept_whole() -> None:
    line = parse_text_line(4, "something odd happened", LogFormat.PLAIN, YEAR)
    assert line.message == "something odd happened"
    assert line.raw == "something odd happened"
    assert line.level is LogLevel.UNKNOWN
    assert line.timestamp is None


def test_fallback_to_another_format() -> None:
    line = parse_text_line(1, NGINX, LogFormat.PLAIN, YEAR)
    assert line.error_code == "502"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([APP] * 5, LogFormat.PLAIN),
        ([NGINX] * 5, LogFormat.NGINX_ACCESS),
        ([NGINX_ERR] * 5, LogFormat.NGINX_ERROR),
        ([SYSLOG.replace("kernel", "sshd")] * 5, LogFormat.SYSLOG),
        ([SYSLOG, KUBELET], LogFormat.KUBERNETES),
        (["2026-10-09T14:00:00Z stdout F hello"] * 3, LogFormat.KUBERNETES),
        (["free text", "more free text", APP], LogFormat.PLAIN),
        ([], LogFormat.PLAIN),
    ],
)
def test_detect_text_format(lines: list[str], expected: LogFormat) -> None:
    assert detect_text_format(lines) is expected


# ------------------------------------------------------------------ structured records


def test_column_mapping_with_odd_names() -> None:
    mapper = ColumnMapper()
    fields = {
        "@Timestamp": "2026-10-09T15:01:02Z",
        "SEVERITY": "error",
        "App": "checkout",
        "Msg": "Connection refused",
        "http.status": "503",
        "RequestID": "c-1001",
        "region": "eu-1",
    }
    line = parse_record(3, fields, "raw", mapper, YEAR)
    assert line.level is LogLevel.ERROR
    assert line.service == "checkout"
    assert line.message == "Connection refused"
    assert line.error_code == "503"
    assert line.trace_id == "c-1001"
    assert line.extra == {"region": "eu-1"}
    assert mapper.mapping == {
        "timestamp": "@Timestamp",
        "level": "SEVERITY",
        "service": "App",
        "message": "Msg",
        "error_code": "http.status",
        "trace_id": "RequestID",
    }


def test_missing_message_column_builds_key_values() -> None:
    line = parse_record(
        1, {"status": "502", "path": "/x", "empty": ""}, "raw", ColumnMapper(), 2026
    )
    assert line.message == "path=/x"
    assert line.level is LogLevel.ERROR


def test_record_with_nothing_mapped_uses_raw() -> None:
    line = parse_record(1, {"level": "info"}, '{"level":"info"}', ColumnMapper(), YEAR)
    assert line.message == '{"level":"info"}'


def test_empty_columns_are_skipped_in_mapping() -> None:
    line = parse_record(1, {"msg": "", "message": "real"}, "raw", ColumnMapper(), YEAR)
    assert line.message == "real"
