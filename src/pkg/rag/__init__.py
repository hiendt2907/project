"""Unified RAG gate (Postgres pgvector) — single entry for alert/evidence RAG-before-LLM."""

from pkg.rag.gate import RagGateOutcome, evaluate_rag_gate, normalize_rag_query

__all__ = [
    "RagGateOutcome",
    "evaluate_rag_gate",
    "normalize_rag_query",
]
