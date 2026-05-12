"""SIEMEvidenceAdapter — converts a FinGuard incident dict into Omni evidence envelopes.

Omni receives raw SIEM events from the siem_bridge (via omni-alerts → prober). This adapter
produces synthetic evidence envelopes that skip the prober layer and inject SIEM context
directly into the analyst evidence pipeline (omni-diagnostic-evidence).

Usage:
    adapter = SIEMEvidenceAdapter()
    envelopes = adapter.to_evidence(finguard_incident_fields)
    for env in envelopes:
        await kafka.send_dict("omni-diagnostic-evidence", {"data": json.dumps(env)})
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


# Maps FinGuard severity to Omni-canonical severity.
_SEVERITY_MAP = {
    "critical": "critical",
    "high": "warning",
    "medium": "warning",
    "low": "info",
    "info": "info",
}

# Maps FinGuard category to Prometheus-style alertname.
_CATEGORY_TO_ALERTNAME = {
    "network_anomaly": "SIEMNetworkAnomaly",
    "auth_failure": "SIEMAuthFailure",
    "malware": "SIEMMalwareDetected",
    "ddos": "SIEMDDoSDetected",
    "data_exfil": "SIEMDataExfiltration",
    "lateral_movement": "SIEMLateralMovement",
    "k8s_threat": "SIEMKubernetesThreat",
}

# Probe IDs produced by this adapter.
_PROBE_SIEM_INCIDENT = "siem_incident"
_PROBE_SIEM_NETWORK = "siem_network_event"


class SIEMEvidenceAdapter:
    """Converts FinGuard incident dicts to Omni evidence envelopes."""

    def to_evidence(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a list of evidence envelopes for one FinGuard incident."""
        incident_id = str(event.get("id") or uuid.uuid4())
        severity = str(event.get("severity") or "medium").lower()
        category = str(event.get("category") or "unknown").lower()
        tenant_id = str(event.get("tenant_id") or "unknown")
        description = str(event.get("description") or event.get("message") or "")
        suggested_action = str(event.get("suggested_action") or "")
        affected_ip = str(event.get("affected_ip") or "")
        playbook_id = str(event.get("siem_playbook_id") or "")
        hitl_required = bool(event.get("hitl_required"))

        omni_severity = _SEVERITY_MAP.get(severity, "warning")
        alert_rule = _CATEGORY_TO_ALERTNAME.get(category, f"SIEM{category.replace('_', '').title()}")
        trace_id = f"fg-{incident_id[:8]}"
        namespace = f"siem-{tenant_id[:24]}"

        # Canonical query snippet carries SIEM labels so PlaybookMatcher can match.
        canonical_snippet = json.dumps(
            {
                "labels": {
                    "siem_source": "finguard",
                    "siem_category": category,
                    "severity": omni_severity,
                    "siem_playbook_id": playbook_id,
                    "siem_hitl_required": "true" if hitl_required else "false",
                }
            },
            ensure_ascii=False,
        )

        alert_hint = (
            f"FinGuard SIEM incident: category={category} severity={omni_severity} "
            f"tenant={tenant_id} affected_ip={affected_ip or 'unknown'}. "
            f"{description[:500]}"
        )

        # Primary evidence envelope: incident summary.
        incident_envelope: dict[str, Any] = {
            "trace_id": trace_id,
            "probe": _PROBE_SIEM_INCIDENT,
            "alert_rule": alert_rule,
            "alert_hint": alert_hint,
            "symptom_group": category,
            "canonical_query_snippet": canonical_snippet,
            "lane": "SIEM_SECURITY",
            "stream_tags": ["SIEM_SECURITY"],
            "extracted_fact": {
                "incident_id": incident_id,
                "category": category,
                "severity": omni_severity,
                "tenant_id": tenant_id,
                "affected_ip": affected_ip,
                "suggested_action": suggested_action,
                "hitl_required": hitl_required,
                "playbook_id": playbook_id,
                "namespace": namespace,
            },
            "raw": json.dumps(
                {k: v for k, v in event.items() if k not in ("raw_log",)},
                ensure_ascii=False,
                default=str,
            )[:8000],
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

        envelopes: list[dict[str, Any]] = [incident_envelope]

        # Optional secondary envelope: network event context when affected_ip is present.
        if affected_ip:
            raw_log = str(event.get("raw_log") or "")
            network_envelope: dict[str, Any] = {
                "trace_id": trace_id,
                "probe": _PROBE_SIEM_NETWORK,
                "alert_rule": alert_rule,
                "alert_hint": f"Network context: affected_ip={affected_ip}. {raw_log[:400]}",
                "symptom_group": category,
                "canonical_query_snippet": canonical_snippet,
                "lane": "SIEM_SECURITY",
                "stream_tags": ["SIEM_SECURITY"],
                "extracted_fact": {
                    "affected_ip": affected_ip,
                    "raw_log_excerpt": raw_log[:2000],
                    "tenant_id": tenant_id,
                    "namespace": namespace,
                },
                "raw": raw_log[:4000],
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }
            envelopes.append(network_envelope)

        return envelopes
