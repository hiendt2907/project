"""EvidenceAdapter Protocol — contract for converting raw external events into Omni evidence envelopes.

Any class implementing this protocol can be passed to AdapterGeneratorWorker.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvidenceAdapter(Protocol):
    """Transform a raw external event dict into one or more Omni evidence envelope dicts.

    Each returned envelope is a single diagnostic evidence document compatible with
    the ``omni-diagnostic-evidence`` Kafka topic schema:

    Required keys:
        trace_id (str)         — unique per incident; correlates all probes for one alert
        probe    (str)         — logical probe ID (e.g. "siem_incident", "siem_network_event")
        alert_rule (str)       — Prometheus-style alertname (e.g. "SIEMNetworkAnomaly")
        alert_hint (str)       — human-readable hint for the analyst LLM
        extracted_fact (dict)  — structured facts for the analyst (namespace-safe; no PII)
        raw      (str)         — raw payload text (truncated, for audit)

    Optional keys:
        symptom_group (str)                — incident category tag
        canonical_query_snippet (str)      — JSON with siem_source labels for PlaybookMatcher
    """

    def to_evidence(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert a raw event into a list of Omni evidence envelopes."""
        ...
