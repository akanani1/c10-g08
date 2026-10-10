"""The example logs in docs/templates parse the way their labels say.

Each labelled evidence line must survive the pre-filter (WARN or worse), and each labelled
context line must be kept as context or as a change event. When the real sample logs land in
sample_logs/, point EXAMPLES at that folder too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from incident_suite.parsing.grouping import group_lines
from incident_suite.parsing.readers import parse_upload
from incident_suite.state import LogLevel

TEMPLATES = Path(__file__).resolve().parents[2] / "docs" / "templates" / "gulam"
EXAMPLES = TEMPLATES / "example_logs"
LABELS: list[dict[str, Any]] = json.loads(
    (TEMPLATES / "labels.example.json").read_text(encoding="utf-8")
)["logs"]
KEPT = {LogLevel.WARN, LogLevel.ERROR, LogLevel.FATAL}


@pytest.mark.parametrize("label", LABELS, ids=[label["file"] for label in LABELS])
def test_example_log_matches_its_labels(label: dict[str, Any]) -> None:
    path = EXAMPLES / label["file"]
    parsed = parse_upload(path.read_bytes(), path.name, year=2026)
    assert parsed.format.value == label["format"]

    by_number = {line.line_no: line for line in parsed.lines}
    for issue in label["expected_issues"]:
        for number in issue["evidence_lines"]:
            assert by_number[number].level in KEPT, f"line {number} was not kept"

    grouped = group_lines(parsed.lines)
    kept = {line.line_no for line in grouped.context_lines + grouped.change_lines}
    kept |= {line.line_no for line in parsed.lines if line.level in KEPT}
    missing = set(label.get("context_lines", [])) - kept
    assert not missing, f"context lines dropped: {sorted(missing)}"
