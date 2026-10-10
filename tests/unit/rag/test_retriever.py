"""Tests for incident_suite/rag/retriever.py, with offline embeddings."""

from __future__ import annotations

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from incident_suite.rag.retriever import RunbookRetriever


def _doc(runbook: str, text: str) -> Document:
    return Document(
        page_content=text, metadata={"runbook": runbook, "title": runbook, "section": "Fix now"}
    )


def test_best_match_first(embeddings: Embeddings) -> None:
    retriever = RunbookRetriever(
        [
            _doc("disk-full", "No space left on device, disk full, clean volumes"),
            _doc("db-pool", "Connection refused to database, pool exhausted"),
            _doc("tls", "Certificate expired, TLS handshake failed"),
        ],
        embeddings,
    )
    hits = retriever.search("database connection refused", k=2)
    assert retriever.size == 3
    assert [hit.id for hit in hits] == ["R1", "R2"]
    assert hits[0].runbook == "db-pool"
    assert hits[0].section == "Fix now"
    assert hits[0].score > hits[1].score
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_empty_store(embeddings: Embeddings) -> None:
    retriever = RunbookRetriever([], embeddings)
    assert retriever.size == 0
    assert retriever.search("anything", k=3) == []


def test_unrelated_text_scores_zero(embeddings: Embeddings) -> None:
    retriever = RunbookRetriever([_doc("dns", "NXDOMAIN resolution")], embeddings)
    (hit,) = retriever.search("payment checkout latency", k=1)
    assert hit.score == 0.0
