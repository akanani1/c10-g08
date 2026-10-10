"""The 17 sample logs parse the way labels.json says, and match what make_samples.py writes.

Each labelled evidence line must survive the pre-filter (WARN or worse), each context line must
be kept as context or as a change event, and the repeat log must share the original's signature.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from incident_suite.parsing.grouping import group_lines
from incident_suite.parsing.readers import parse_upload
from incident_suite.state import GroupedLog, LogLevel, ParsedLog

SAMPLES = Path(__file__).resolve().parents[2] / "sample_logs"
LABELS: list[dict[str, Any]] = json.loads((SAMPLES / "labels.json").read_text(encoding="utf-8"))[
    "logs"
]
KEPT = {LogLevel.WARN, LogLevel.ERROR, LogLevel.FATAL}


@cache
def _load(name: str) -> tuple[ParsedLog, GroupedLog]:
    parsed = parse_upload((SAMPLES / name).read_bytes(), name, year=2026)
    return parsed, group_lines(parsed.lines)


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_samples", SAMPLES / "make_samples.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


def test_there_are_17_logs_in_every_format() -> None:
    assert len(LABELS) == 17
    assert {label["format"] for label in LABELS} == {
        "plain", "syslog", "kubernetes", "nginx_access", "nginx_error",
        "json_lines", "json_document", "csv", "tsv", "parquet",
    }  # fmt: skip
    assert any(label["compressed"] for label in LABELS)


@pytest.mark.parametrize("label", LABELS, ids=[label["file"] for label in LABELS])
def test_sample_log_matches_its_labels(label: dict[str, Any]) -> None:
    parsed, grouped = _load(label["file"])
    assert parsed.format.value == label["format"]
    assert parsed.compressed == label["compressed"]

    by_number = {line.line_no: line for line in parsed.lines}
    for issue in label["expected_issues"]:
        for number in issue["evidence_lines"]:
            assert by_number[number].level in KEPT, f"line {number} was not kept"
    for number in label["noise_lines"]:
        assert by_number[number].level in KEPT, f"noise line {number} is not a warning"

    kept = {line.line_no for line in grouped.context_lines + grouped.change_lines}
    kept |= {line.line_no for line in parsed.lines if line.level in KEPT}
    missing = set(label["context_lines"]) - kept
    assert not missing, f"context lines dropped: {sorted(missing)}"


def test_the_repeat_log_shares_the_original_signature() -> None:
    (repeat,) = [label for label in LABELS if "same_signature_as" in label]
    original = {p.signature_hash for p in _load(repeat["same_signature_as"])[1].patterns}
    again = {p.signature_hash for p in _load(repeat["file"])[1].patterns}
    assert original & again


def test_the_large_log_has_50k_rows() -> None:
    parsed, grouped = _load("large_50k_mixed.parquet")
    assert len(parsed.lines) == 50_000
    assert 10 <= len(grouped.patterns) <= 30


def test_committed_files_match_the_generator() -> None:
    for name, data in _generator().build().items():
        assert (SAMPLES / name).read_bytes() == data, f"{name} is stale: run make_samples.py"
