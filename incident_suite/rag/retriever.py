"""Runbook search: LangChain ``InMemoryVectorStore`` with OpenRouter embeddings.

The store is built once per app start (the UI caches it with ``st.cache_resource``), from about
15 short runbooks. Scores are cosine similarities, clamped to 0..1 for display and thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore


@dataclass(frozen=True)
class RunbookHit:
    """One matching runbook section. ``id`` (R1, R2 ...) is how the model cites it."""

    id: str
    runbook: str
    title: str
    section: str
    text: str
    score: float


class RunbookRetriever:
    """Top-k search over runbook sections."""

    def __init__(self, documents: list[Document], embeddings: Embeddings) -> None:
        self._store = InMemoryVectorStore(embeddings)
        self._size = len(documents)
        if documents:
            self._store.add_documents(documents)

    @property
    def size(self) -> int:
        """Number of runbook sections indexed."""
        return self._size

    def search(self, query: str, k: int) -> list[RunbookHit]:
        """The ``k`` best sections for ``query``, best first."""
        if not self._size:
            return []
        results = self._store.similarity_search_with_score(query, k=k)
        return [
            RunbookHit(
                id=f"R{rank}",
                runbook=str(doc.metadata.get("runbook", "")),
                title=str(doc.metadata.get("title", "")),
                section=str(doc.metadata.get("section", "")),
                text=doc.page_content,
                score=min(1.0, max(0.0, float(score))),
            )
            for rank, (doc, score) in enumerate(results, start=1)
        ]
