"""Tests for the ``python -m incident_suite.parsing`` inspector."""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_suite.parsing.__main__ import main


def test_summary_for_a_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "rate.csv"
    log.write_text(
        "time,level,service,message,api_key\n"
        "2026-10-09T10:00:00Z,INFO,notify,Deploy finished v2,x\n"
        "2026-10-09T10:00:01Z,WARN,notify,429 from provider,abc\n",
        encoding="utf-8",
    )
    assert main([str(log)]) == 0
    out = capsys.readouterr().out
    assert "rate.csv: Detected CSV, 5 columns, 2 lines" in out
    assert "message <- message" in out
    assert "redacted: 2 credential_pair" in out
    assert "WARN  x1" in out
    assert "1: Deploy finished v2" in out


def test_warnings_are_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "bad.csv"
    log.write_text("a,b\n1,2,3\n", encoding="utf-8")
    assert main([str(log)]) == 0
    assert "warning: 1 rows had a different number" in capsys.readouterr().out


def test_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "nope.log")]) == 1
    assert "error:" in capsys.readouterr().err


def test_unreadable_content(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "empty.log"
    log.write_text("\n", encoding="utf-8")
    assert main([str(log)]) == 1
    assert "empty" in capsys.readouterr().err
