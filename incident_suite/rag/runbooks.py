"""Load the markdown runbooks and split them into searchable sections.

Each runbook has YAML front matter (``id``, ``title``, ``categories``, ``signals``) and ``##``
sections, as in ``docs/templates/gulam/runbook_template.md``. Every section becomes one search
document, prefixed with the runbook title and signals so a section matches on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from langchain_core.documents import Document

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "data" / "runbooks"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Runbook:
    """One runbook: metadata plus its ``(heading, body)`` sections."""

    id: str
    title: str
    categories: tuple[str, ...]
    signals: tuple[str, ...]
    sections: tuple[tuple[str, str], ...]


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def parse_runbook(text: str, fallback_id: str) -> Runbook:
    """Parse one runbook file. Bad front matter is ignored rather than failing the app."""
    meta: dict[str, Any] = {}
    match = _FRONT_MATTER.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match[1])
        except yaml.YAMLError:
            loaded = None
        meta = loaded if isinstance(loaded, dict) else {}
        text = text[match.end() :]
    text = _COMMENT.sub("", text)
    heading = _TITLE.search(text)
    parts = _SECTION.split(text)
    sections = tuple(
        (name.strip(), body.strip())
        for name, body in zip(parts[1::2], parts[2::2], strict=True)
        if body.strip()
    )
    return Runbook(
        id=str(meta.get("id") or fallback_id),
        title=str(meta.get("title") or (heading[1] if heading else fallback_id)),
        categories=_strings(meta.get("categories")),
        signals=_strings(meta.get("signals")),
        sections=sections,
    )


def load_runbooks(directory: Path = RUNBOOK_DIR) -> list[Runbook]:
    """Every ``*.md`` runbook in ``directory``, sorted by file name. Missing folder: none."""
    if not directory.is_dir():
        return []
    return [
        parse_runbook(path.read_text(encoding="utf-8"), path.stem)
        for path in sorted(directory.glob("*.md"))
    ]


def to_documents(runbooks: list[Runbook]) -> list[Document]:
    """One search document per runbook section."""
    documents = []
    for runbook in runbooks:
        signals = "; ".join(runbook.signals)
        for heading, body in runbook.sections:
            text = f"{runbook.title}: {heading}\n"
            if signals:
                text += f"Signals: {signals}\n"
            documents.append(
                Document(
                    page_content=text + body,
                    metadata={"runbook": runbook.id, "title": runbook.title, "section": heading},
                )
            )
    return documents


REQUIRED_SECTIONS = ("Symptoms", "Likely causes", "Diagnose", "Fix now", "Prevent", "References")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")


def check_runbook(runbook: Runbook, known_categories: frozenset[str]) -> list[str]:
    """What's wrong with a runbook, or nothing. Our own runbooks and AI drafts both pass this."""
    problems = []
    if not _ID.fullmatch(runbook.id):
        problems.append("the id must be lowercase letters, digits and dashes")
    if not runbook.title.strip():
        problems.append("it needs a title")
    if not runbook.categories or not set(runbook.categories) <= known_categories:
        problems.append("it needs at least one known category")
    if not runbook.signals:
        problems.append("it needs at least one signal")
    present = {name for name, _ in runbook.sections}
    missing = [name for name in REQUIRED_SECTIONS if name not in present]
    if missing:
        problems.append("missing sections: " + ", ".join(missing))
    return problems
