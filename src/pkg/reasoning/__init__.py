"""RAG / LLM reasoning boundary — no imports from ``pkg.executor``.

Runtime wiring: ``workers.evidence_consumer.reason_from_diagnostic_evidence`` → ``reason_diagnostic_evidence_only`` (no ``pkg.executor``).
"""

from pkg.reasoning.schema import (
    DiagnosticEvidenceDict,
    OmniActionEnvelope,
    OmniActionKafkaBody,
    coerce_evidence_dict,
)
from pkg.reasoning.sanitize import evidence_relevance_warning, format_sanitized_analyst_user_text

__all__ = [
    "DiagnosticEvidenceDict",
    "OmniActionEnvelope",
    "OmniActionKafkaBody",
    "coerce_evidence_dict",
    "evidence_relevance_warning",
    "format_sanitized_analyst_user_text",
]
