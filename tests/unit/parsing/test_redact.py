"""Tests for incident_suite/parsing/redact.py: a positive and a negative case per pattern.

The secrets here are fake and only look real enough for the patterns to match.
"""

from __future__ import annotations

import pytest

from incident_suite.parsing.redact import Redactor
from incident_suite.state import RedactionType

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLXRlc3Q"  # gitleaks:allow (fake)
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("user alice@example.com failed", RedactionType.EMAIL, "user [REDACTED:email] failed"),
        (f"token {FAKE_JWT} ok", RedactionType.JWT, "token [REDACTED:jwt] ok"),
        (
            "Authorization: Bearer abcdef1234567890",
            RedactionType.BEARER_TOKEN,
            "Authorization: Bearer [REDACTED:bearer_token]",
        ),
        (
            f"key {FAKE_AWS} used",
            RedactionType.AWS_ACCESS_KEY,
            "key [REDACTED:aws_access_key] used",
        ),
        (
            "connect password=hunter2 host=db",
            RedactionType.CREDENTIAL_PAIR,
            "connect password=[REDACTED:credential_pair] host=db",
        ),
        (
            '{"api_key": "sk-123456"}',
            RedactionType.CREDENTIAL_PAIR,
            '{"api_key": [REDACTED:credential_pair]}',
        ),
        (
            "DB_PASSWORD: 's3cret value'",
            RedactionType.CREDENTIAL_PAIR,
            "DB_PASSWORD: [REDACTED:credential_pair]",
        ),
        (
            "x-api-key=abc123&page=2",
            RedactionType.CREDENTIAL_PAIR,
            "x-api-key=[REDACTED:credential_pair]&page=2",
        ),
    ],
)
def test_each_pattern_masks(text: str, kind: RedactionType, expected: str) -> None:
    redactor = Redactor()
    assert redactor.redact(text) == expected
    assert redactor.summary() == {kind: 1}


@pytest.mark.parametrize(
    "text",
    [
        "Connection refused to payments-db.internal:5432",
        "user@ (no domain) logged out",
        "Bearer short",
        "AKIA123 is too short",
        "eyJhbGciOiJIUzI1NiJ9 alone is not a JWT",
        "the author: Jane",
        "passed=true checks=12",
        "token count 400, tokens=5",
        "trace=7f3a01da request_id=r-91a2",
        "[REDACTED:email] already",
    ],
)
def test_harmless_text_is_untouched(text: str) -> None:
    redactor = Redactor()
    assert redactor.redact(text) == text
    assert redactor.summary() == {}


def test_bearer_with_jwt_counts_once_as_jwt() -> None:
    redactor = Redactor()
    assert redactor.redact(f"Authorization: Bearer {FAKE_JWT}") == (
        "Authorization: Bearer [REDACTED:jwt]"
    )
    assert redactor.summary() == {RedactionType.JWT: 1}


def test_private_key_in_one_string() -> None:
    begin, end = "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"
    text = f"key={begin}\nMIIabc\n{end} done"  # gitleaks:allow
    redactor = Redactor()
    assert "MIIabc" not in redactor.redact(text)
    assert redactor.summary()[RedactionType.PRIVATE_KEY] == 1


def test_private_key_across_lines() -> None:
    redactor = Redactor()
    lines = [
        "loading key -----BEGIN PRIVATE KEY-----",
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
        "-----END PRIVATE KEY-----",
        "next line password=x",
    ]
    out = [redactor.redact_line(line) for line in lines]
    assert out == [
        "loading key [REDACTED:private_key]",
        "[REDACTED:private_key]",
        "[REDACTED:private_key]",
        "next line password=[REDACTED:credential_pair]",
    ]
    assert redactor.summary() == {
        RedactionType.PRIVATE_KEY: 1,
        RedactionType.CREDENTIAL_PAIR: 1,
    }


def test_secret_named_columns_are_masked_whole() -> None:
    redactor = Redactor()
    assert redactor.redact_field("db.password", "plain text") == "[REDACTED:credential_pair]"
    assert redactor.redact_field("Authorization", "Basic abc") == "[REDACTED:credential_pair]"
    assert redactor.redact_field("message", "ok") == "ok"
    assert redactor.redact_field("token", "") == ""
    assert redactor.summary() == {RedactionType.CREDENTIAL_PAIR: 2}


def test_disabled_types_are_left_alone() -> None:
    redactor = Redactor(enabled={RedactionType.EMAIL})
    text = f"a@example.com {FAKE_AWS} password=x -----BEGIN PRIVATE KEY-----"  # gitleaks:allow
    assert redactor.redact(text) == (
        f"[REDACTED:email] {FAKE_AWS} password=x -----BEGIN PRIVATE KEY-----"
    )
    assert redactor.redact_line("-----BEGIN PRIVATE KEY-----") == "-----BEGIN PRIVATE KEY-----"
    assert redactor.redact_field("password", "x") == "x"
