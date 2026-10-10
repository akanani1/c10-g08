"""Runbook search: LangChain ``InMemoryVectorStore`` with OpenRouter embeddings.

Runbooks are indexed section by section, because a single section gives a sharp match score.
Results are then collapsed to distinct runbooks and returned in full, so Remediation always sees
the "Fix now" steps, not just the section that happened to match the error text best.

The store is built once per app start (the UI caches it with ``st.cache_resource``), from about
15 short runbooks. Scores are cosine similarities, clamped to 0..1 for display and thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

from incident_suite.rag.runbooks import Runbook, to_documents


@dataclass(frozen=True)
class RunbookHit:
    """One matching runbook, in full. ``id`` (R1, R2 ...) is how the model cites it."""

    id: str
    runbook: str
    title: str
    matched_section: str
    text: str
    score: float


def full_text(runbook: Runbook) -> str:
    """The whole runbook as the model reads it: title, signals and every section."""
    parts = [f"# {runbook.title}"]
    if runbook.signals:
        parts.append("Signals: " + "; ".join(runbook.signals))
    parts += [f"## {heading}\n{body}" for heading, body in runbook.sections]
    return "\n\n".join(parts)


class RunbookRetriever:
    """Top-k distinct runbooks for a query, ranked by their best-matching section."""

    def __init__(self, runbooks: Sequence[Runbook], embeddings: Embeddings) -> None:
        self._store = InMemoryVectorStore(embeddings)
        self._runbooks = {runbook.id: runbook for runbook in runbooks}
        documents = to_documents(list(runbooks))
        self._sections = len(documents)
        if documents:
            self._store.add_documents(documents)

    @property
    def size(self) -> int:
        """Number of runbooks indexed."""
        return len(self._runbooks)

    def search(self, query: str, k: int) -> list[RunbookHit]:
        """The ``k`` best runbooks for ``query``, best first, each with its full text."""
        if not self._sections:
            return []
        best: dict[str, tuple[str, float]] = {}
        for doc, score in self._store.similarity_search_with_score(query, k=self._sections):
            runbook_id = str(doc.metadata.get("runbook", ""))
            if runbook_id in self._runbooks and runbook_id not in best:
                best[runbook_id] = (str(doc.metadata.get("section", "")), float(score))
            if len(best) == k:
                break
        return [
            RunbookHit(
                id=f"R{rank}",
                runbook=runbook_id,
                title=self._runbooks[runbook_id].title,
                matched_section=section,
                text=full_text(self._runbooks[runbook_id]),
                score=min(1.0, max(0.0, score)),
            )
            for rank, (runbook_id, (section, score)) in enumerate(best.items(), start=1)
        ]
