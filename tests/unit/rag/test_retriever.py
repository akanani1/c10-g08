"""Tests for incident_suite/rag/retriever.py, with offline embeddings."""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from incident_suite.rag.retriever import RunbookRetriever, full_text
from incident_suite.rag.runbooks import Runbook


def _runbook(runbook_id: str, symptoms: str, fix: str) -> Runbook:
    return Runbook(
        id=runbook_id,
        title=runbook_id.replace("-", " ").title(),
        categories=(),
        signals=(symptoms,),
        sections=(("Symptoms", symptoms), ("Fix now", fix)),
    )


RUNBOOKS = [
    _runbook("disk-full", "No space left on device, disk full", "Free space on the volume."),
    _runbook("db-pool", "Connection refused to database, pool exhausted", "Raise the pool size."),
    _runbook("tls", "Certificate expired, TLS handshake failed", "Renew the certificate."),
]


def test_returns_distinct_runbooks_in_full(embeddings: Embeddings) -> None:
    retriever = RunbookRetriever(RUNBOOKS, embeddings)
    hits = retriever.search("database connection refused", k=2)
    assert retriever.size == 3
    assert [hit.id for hit in hits] == ["R1", "R2"]
    assert len({hit.runbook for hit in hits}) == 2
    best = hits[0]
    assert best.runbook == "db-pool"
    assert best.title == "Db Pool"
    assert best.matched_section == "Symptoms"
    assert "## Fix now\nRaise the pool size." in best.text
    assert hits[0].score > hits[1].score
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_k_larger_than_the_store(embeddings: Embeddings) -> None:
    assert len(RunbookRetriever(RUNBOOKS, embeddings).search("disk", k=10)) == 3


def test_empty_store(embeddings: Embeddings) -> None:
    retriever = RunbookRetriever([], embeddings)
    assert retriever.size == 0
    assert retriever.search("anything", k=3) == []


def test_unrelated_text_scores_zero(embeddings: Embeddings) -> None:
    retriever = RunbookRetriever(
        [_runbook("dns", "NXDOMAIN resolution", "Fix the resolver.")], embeddings
    )
    (hit,) = retriever.search("payment checkout latency", k=1)
    assert hit.score == 0.0


def test_full_text_layout() -> None:
    assert full_text(RUNBOOKS[0]) == (
        "# Disk Full\n\nSignals: No space left on device, disk full\n\n"
        "## Symptoms\nNo space left on device, disk full\n\n## Fix now\nFree space on the volume."
    )
