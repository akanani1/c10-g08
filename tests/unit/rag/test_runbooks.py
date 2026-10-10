"""Tests for incident_suite/rag/runbooks.py."""

from __future__ import annotations

from pathlib import Path

from incident_suite.rag.runbooks import load_runbooks, parse_runbook, to_documents

TEMPLATE = Path(__file__).resolve().parents[3] / "docs/templates/gulam/runbook_template.md"


def test_the_runbook_template_parses() -> None:
    runbook = parse_runbook(TEMPLATE.read_text(encoding="utf-8"), "fallback")
    assert runbook.id == "db-pool-exhausted"
    assert runbook.title == "Database connection pool exhausted"
    assert runbook.categories == ("database",)
    assert "Connection refused to <db-host>:<port>" in runbook.signals
    assert [name for name, _ in runbook.sections] == [
        "Symptoms",
        "Likely causes",
        "Diagnose",
        "Fix now",
        "Prevent",
        "References",
    ]
    assert all("HOW TO USE" not in body for _, body in runbook.sections)


def test_missing_or_bad_front_matter_falls_back() -> None:
    plain = parse_runbook("# Disk full\n\n## Fix now\nDelete old logs.\n\n## Empty\n", "disk")
    assert (plain.id, plain.title) == ("disk", "Disk full")
    assert plain.sections == (("Fix now", "Delete old logs."),)
    broken = parse_runbook("---\n: [unclosed\n---\n## Fix\nx\n", "broken")
    assert broken.id == "broken"
    listy = parse_runbook("---\n- a list\n---\n## Fix\nx\n", "listy")
    assert listy.title == "listy"


def test_signals_as_a_single_string() -> None:
    runbook = parse_runbook("---\nsignals: OOMKilled\ncategories: memory\n---\n## Fix\nx", "oom")
    assert runbook.signals == ("OOMKilled",)
    assert runbook.categories == ("memory",)


def test_load_and_split(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text("# B\n## Fix now\nRestart.\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    runbooks = load_runbooks(tmp_path)
    assert [r.id for r in runbooks] == ["db-pool-exhausted", "b"]
    documents = to_documents(runbooks)
    assert len(documents) == 7
    first = documents[0]
    assert first.page_content.startswith("Database connection pool exhausted: Symptoms\nSignals: ")
    assert first.metadata == {
        "runbook": "db-pool-exhausted",
        "title": "Database connection pool exhausted",
        "section": "Symptoms",
    }
    assert documents[-1].page_content == "B: Fix now\nRestart."


def test_missing_folder_means_no_runbooks(tmp_path: Path) -> None:
    assert load_runbooks(tmp_path / "nope") == []
