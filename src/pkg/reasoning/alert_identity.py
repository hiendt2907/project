"""Parse Prometheus alert labels into structured Signal DNA and helpers (Golden Link)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Kubernetes label keys (subset used by Omni)
OMNI_SYMPTOM_GROUP = "omni.io/symptom-group"
OMNI_LAYER = "omni.io/layer"
OMNI_VERIFY_REQUIRED = "omni_verify_required"


def _norm(s: Any) -> str:
    return str(s or "").strip()


def parse_omni_verify_required(raw: str | None) -> bool | None:
    """Return True/False from alert label, or None if absent / unknown."""
    if raw is None:
        return None
    t = raw.strip().lower()
    if t in ("false", "0", "no", "off"):
        return False
    if t in ("true", "1", "yes", "on"):
        return True
    return None


@dataclass
class SignalDNA:
    """Structured view of Prometheus alert labels for routing and Redis context."""

    alertname: str = ""
    severity: str = ""
    namespace: str = ""
    deployment: str = ""
    drift_type: str = ""
    service_account: str = ""
    symptom_group: str = ""
    omni_layer: str = ""
    omni_verify_required: bool | None = None

    def identity_bits_for_error_hint(self) -> list[str]:
        bits: list[str] = []
        if self.alertname:
            bits.append(f"alertname={self.alertname}")
        if self.namespace:
            bits.append(f"namespace={self.namespace}")
        if self.deployment:
            bits.append(f"deployment={self.deployment}")
        if self.drift_type:
            bits.append(f"drift_type={self.drift_type}")
        if self.symptom_group:
            bits.append(f"symptom_group={self.symptom_group}")
        if self.omni_layer:
            bits.append(f"layer={self.omni_layer}")
        if self.service_account:
            bits.append(f"service_account={self.service_account}")
        return bits


def parse_signal_dna_from_labels(labels: dict[str, str]) -> SignalDNA:
    """Map flat alert labels (string values) to SignalDNA."""
    sg = _norm(labels.get(OMNI_SYMPTOM_GROUP) or labels.get("symptom_group"))
    dep = _norm(labels.get("deployment"))
    if not dep:
        dep = _norm(labels.get("deployment_name"))
    if not dep:
        dep = _norm(labels.get("workload"))
    return SignalDNA(
        alertname=_norm(labels.get("alertname")),
        severity=_norm(labels.get("severity")),
        namespace=_norm(labels.get("namespace")),
        deployment=dep,
        drift_type=_norm(labels.get("drift_type")),
        service_account=_norm(labels.get("service_account")),
        symptom_group=sg,
        omni_layer=_norm(labels.get(OMNI_LAYER)),
        omni_verify_required=parse_omni_verify_required(labels.get(OMNI_VERIFY_REQUIRED)),
    )


def labels_dict_from_canonical_query_snippet(snip: str) -> dict[str, str]:
    """Extract string labels from embedded canonical_query JSON snippet if present."""
    s = (snip or "").strip()
    if not s.startswith("{"):
        return {}
    try:
        j = json.loads(s)
        labels = j.get("labels") if isinstance(j, dict) else None
        if not isinstance(labels, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in labels.items():
            if v is None or isinstance(v, (dict, list)):
                continue
            out[str(k)] = str(v).strip()
        return out
    except Exception:
        return {}


def infer_root_cause_id(drift_type: str, alertname: str) -> str:
    """Stable taxonomy id for RAG metadata (snake_case)."""
    dt = re.sub(r"[^a-zA-Z0-9]+", "_", (drift_type or "").strip()).strip("_").lower()
    if dt:
        return f"{dt}"
    an = re.sub(r"[^a-zA-Z0-9]+", "_", (alertname or "").strip()).strip("_").lower()
    return an or "unknown"


def resolution_labels_payload(
    *,
    root_cause_id: str,
    root_cause_desc: str,
    resolution_tool: str,
    verify_method: str,
) -> dict[str, str]:
    """Metadata dict for experience store (matches plan Resolution DNA)."""
    return {
        "omni.io/root-cause-id": root_cause_id[:256],
        "omni.io/root-cause-desc": root_cause_desc[:2000],
        "omni.io/resolution-tool": resolution_tool[:256],
        "omni.io/verify-method": verify_method[:128],
    }
