"""Tests for incident_suite/parsing/pipeline.py: read, redact, parse together."""

from __future__ import annotations

import gzip
import io
import random
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from incident_suite.parsing.grouping import group_lines
from incident_suite.parsing.pipeline import fingerprint, parse_upload
from incident_suite.parsing.readers import ReadError
from incident_suite.state import LogFormat, LogLevel, RedactionType

PLANTED = {
    "email": "ops.lead@example.com",
    "aws": "AKIA" + "ZZZZYYYYXXXXWWWW",
    "password": "Pa55word!",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwbGFudGVkIn0.cGxhbnRlZC1zaWc",  # gitleaks:allow
}


def test_text_log_end_to_end() -> None:
    data = (
        b"2026-10-09T14:00:09Z INFO deployer [trace=-] Deploy started\n"
        b"2026-10-09T14:01:14Z ERROR api [trace=a1] Connection refused to db:5432\n"
        b"java.net.ConnectException: refused\n"
        b"    at com.example.Db.connect(Db.java:42)\n"
    )
    parsed = parse_upload(data, "app.log", year=2026)
    assert parsed.format is LogFormat.PLAIN
    assert parsed.note == "Detected app log"
    assert parsed.fingerprint == fingerprint(data)
    assert [line.line_no for line in parsed.lines] == [1, 2, 3, 4]
    stack = parsed.lines[3]
    assert stack.level is LogLevel.ERROR
    assert stack.service == "api"


def test_secrets_never_reach_the_parsed_log() -> None:
    text = (
        f"2026-10-09T14:00:00Z ERROR api login failed for {PLANTED['email']}\n"
        f"2026-10-09T14:00:01Z WARN api using key {PLANTED['aws']} password={PLANTED['password']}\n"
        f"2026-10-09T14:00:02Z ERROR api Authorization: Bearer {PLANTED['jwt']}\n"
    )
    parsed = parse_upload(text.encode(), "secrets.log")
    dumped = parsed.model_dump_json() + group_lines(parsed.lines).model_dump_json()
    for secret in PLANTED.values():
        assert secret not in dumped
    assert parsed.redactions == {
        RedactionType.EMAIL: 1,
        RedactionType.AWS_ACCESS_KEY: 1,
        RedactionType.CREDENTIAL_PAIR: 1,
        RedactionType.JWT: 1,
    }


def test_redaction_can_be_switched_off_per_type() -> None:
    data = f"2026-10-09T14:00:00Z ERROR api mail {PLANTED['email']}\n".encode()
    parsed = parse_upload(data, "a.log", redaction=[RedactionType.AWS_ACCESS_KEY])
    assert PLANTED["email"] in parsed.lines[0].message


def test_csv_redacts_every_field_and_builds_raw() -> None:
    data = (
        "time,level,user,password,msg\n"
        f"2026-10-09T10:00:00Z,ERROR,{PLANTED['email']},{PLANTED['password']},login failed\n"
    ).encode()
    parsed = parse_upload(data, "a.csv")
    line = parsed.lines[0]
    assert parsed.format is LogFormat.CSV
    assert parsed.column_mapping["message"] == "msg"
    assert line.extra == {"user": "[REDACTED:email]", "password": "[REDACTED:credential_pair]"}
    assert PLANTED["password"] not in line.raw
    assert line.raw.startswith("2026-10-09T10:00:00Z,ERROR,")


def test_json_lines_with_a_text_line() -> None:
    data = b'{"level": "error", "msg": "boom", "token": "abc"}\n2026-10-09T10:00:00Z WARN x y\n'
    parsed = parse_upload(data, "a.jsonl")
    first, second = parsed.lines
    assert parsed.format is LogFormat.JSON_LINES
    assert first.raw == '{"level":"error","msg":"boom","token":"[REDACTED:credential_pair]"}'
    assert second.level is LogLevel.WARN


def test_gzipped_nginx_log() -> None:
    line = '10.0.0.1 - - [09/Oct/2026:14:20:05 +0000] "GET /x HTTP/1.1" 504 1 "-" "-"\n'
    parsed = parse_upload(gzip.compress((line * 3).encode()), "edge.log.gz")
    assert parsed.format is LogFormat.NGINX_ACCESS
    assert parsed.compressed
    assert parsed.note == "Detected nginx access log (gzip)"


def test_read_errors_propagate() -> None:
    with pytest.raises(ReadError):
        parse_upload(b"", "a.log")


def test_50k_rows_parse_and_group_quickly() -> None:
    rng = random.Random(7)
    services = ["api", "web", "worker", "db"]
    errors = [
        "Connection refused to db.internal:{port}",
        "Timeout after {ms}ms calling search-svc",
        "OOMKilled pod orders-{pod}",
        "Disk /var/data {pct}% full",
    ]
    rows = []
    for i in range(50_000):
        if i % 50 == 0:
            message = rng.choice(errors).format(
                port=rng.randint(1000, 9999),
                ms=rng.randint(1, 9000),
                pod=f"{rng.randint(10000, 99999)}x",
                pct=rng.randint(90, 99),
            )
            level = "ERROR"
        else:
            message, level = f"GET /item/{i} 200", "INFO"
        rows.append(
            {
                "ts": 1791640800 + i,
                "level": level,
                "service": rng.choice(services),
                "message": message,
            }
        )
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)

    started = time.perf_counter()
    parsed = parse_upload(buffer.getvalue(), "large.parquet")
    grouped = group_lines(parsed.lines)
    elapsed = time.perf_counter() - started

    assert len(parsed.lines) == 50_000
    assert grouped.quiet_line_count == 49_000
    assert len(grouped.patterns) == len(services) * len(errors)
    assert elapsed < 30
