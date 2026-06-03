"""RAG gate — single entry point; re-exports from submodule for backward compat."""

from pkg.rag.gate import RagGateOutcome, evaluate_rag_gate, normalize_rag_query

__all__ = [
    "RagGateOutcome",
    "evaluate_rag_gate",
    "normalize_rag_query",
]
