"""Tests for incident_suite/security.py."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from incident_suite.security import (
    RateLimitError,
    RunLimiter,
    UploadError,
    check_upload,
    display_name,
    extension_of,
    passcode_ok,
)


@pytest.mark.parametrize(
    "name",
    [
        "app.log",
        "APP.LOG",
        "a.txt",
        "a.jsonl",
        "a.ndjson",
        "a.json",
        "a.csv",
        "a.tsv",
        "a.parquet",
        "edge.log.gz",
        "data.csv.gz",
    ],
)
def test_allowed_types(name: str) -> None:
    check_upload(name, 100, max_upload_mb=5)


@pytest.mark.parametrize("name", ["a.zip", "a.exe", "a.xlsx", "a.tar.gz", "noextension", "a.gz"])
def test_refused_types(name: str) -> None:
    with pytest.raises(UploadError, match="isn't supported"):
        check_upload(name, 100, max_upload_mb=5)


def test_size_limits() -> None:
    check_upload("a.log", 5 * 1024 * 1024, max_upload_mb=5)
    with pytest.raises(UploadError, match="5 MB"):
        check_upload("a.log", 5 * 1024 * 1024 + 1, max_upload_mb=5)
    with pytest.raises(UploadError, match="empty"):
        check_upload("a.log", 0, max_upload_mb=5)


def test_extension_ignores_gz() -> None:
    assert extension_of("x.LOG.GZ") == "log"
    assert extension_of("README") == ""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd.log", "passwd.log"),
        ("C:\\Users\\me\\app.log", "app.log"),
        ("<script>alert(1)</script>.log", "script_.log"),
        ("...", "upload"),
        ("a" * 300 + ".log", "a" * 120),
    ],
)
def test_display_name(name: str, expected: str) -> None:
    assert display_name(name) == expected


def test_run_limiter() -> None:
    limiter = RunLimiter(max_runs=2, window=60)
    limiter.check(now=0)
    limiter.check(now=10)
    with pytest.raises(RateLimitError, match="Try again in 41 s"):
        limiter.check(now=20)
    limiter.check(now=61)  # the first run has left the window
    with pytest.raises(RateLimitError):
        limiter.check(now=62)
    RunLimiter().check()


def test_passcode_ok() -> None:
    secret = SecretStr("open-sesame")
    assert passcode_ok("open-sesame", secret)
    assert not passcode_ok("open-sesam", secret)
    assert not passcode_ok("", secret)
    assert not passcode_ok("anything", None)
