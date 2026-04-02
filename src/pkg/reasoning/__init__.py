"""RAG / LLM reasoning boundary — no imports from ``pkg.executor``.

Runtime wiring: ``workers.evidence_consumer.reason_from_diagnostic_evidence`` → ``reason_diagnostic_evidence_only`` (no ``pkg.executor``).
"""

from pkg.reasoning.schema import DiagnosticEvidenceDict, coerce_evidence_dict

__all__ = ["DiagnosticEvidenceDict", "coerce_evidence_dict"]
