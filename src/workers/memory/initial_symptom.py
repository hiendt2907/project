"""Structured snapshot of the firing Prometheus / Alertmanager alert (InitialSymptom) for planner prompts."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from pkg.reasoning.alert_identity import labels_dict_from_canonical_query_snippet


class InitialSymptom(BaseModel):
    """Snapshot of the alert that started the trace (Alertmanager / Prometheus shape)."""

    alertname: str = ""
    alert_rule: str = ""
    severity: str = ""
    namespace: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    description: str = ""
    starts_at: str = ""
    generator_url: str = ""
    fingerprint: str = ""
    group_key: str = ""
    error_hint: str = ""
    canonical_query_snippet: str = ""

    def render_for_prompt(self) -> str:
        """Plain text for <INITIAL_SYMPTOM> XML (caller escapes for XML)."""
        lines: list[str] = []
        if self.alertname:
            lines.append(f"alertname: {self.alertname}")
        if self.alert_rule:
            lines.append(f"alert_rule: {self.alert_rule}")
        if self.severity:
            lines.append(f"severity: {self.severity}")
        if self.namespace:
            lines.append(f"namespace: {self.namespace}")
        if self.labels:
            lines.append(f"labels: {json.dumps(self.labels, sort_keys=True)}")
        if self.annotations:
            ann_s = json.dumps(self.annotations, sort_keys=True)
            if len(ann_s) > 1200:
                ann_s = ann_s[:1199] + "…"
            lines.append(f"annotations: {ann_s}")
        if self.summary:
            lines.append(f"summary: {self.summary[:500]}")
        if self.description:
            lines.append(f"description: {self.description[:800]}")
        if self.starts_at:
            lines.append(f"starts_at: {self.starts_at}")
        if self.generator_url:
            lines.append(f"generator_url: {self.generator_url[:500]}")
        if self.fingerprint:
            lines.append(f"fingerprint: {self.fingerprint[:256]}")
        if self.group_key:
            lines.append(f"group_key: {self.group_key[:256]}")
        if self.error_hint:
            lines.append(f"error_hint: {self.error_hint[:500]}")
        if not lines:
            return "(no structured Prometheus alert fields)"
        return "\n".join(lines)


def initial_symptom_from_alertmanager_alert(alert: dict[str, Any]) -> InitialSymptom:
    """Build from one Alertmanager-style alert object (labels + annotations)."""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    ann = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    lbls: dict[str, str] = {}
    for k, v in (labels or {}).items():
        if v is None or isinstance(v, (dict, list)):
            continue
        lbls[str(k)] = str(v).strip()
    anns: dict[str, str] = {}
    for k, v in (ann or {}).items():
        if v is None or isinstance(v, (dict, list)):
            continue
        anns[str(k)] = str(v).strip()
    return InitialSymptom(
        alertname=str(lbls.get("alertname") or lbls.get("alert_name") or ""),
        severity=str(lbls.get("severity") or ""),
        namespace=str(lbls.get("namespace") or lbls.get("ns") or ""),
        labels=lbls,
        annotations=anns,
        summary=str(anns.get("summary") or ""),
        description=str(anns.get("description") or ""),
        starts_at=str(alert.get("startsAt") or alert.get("starts_at") or ""),
        generator_url=str(alert.get("generatorURL") or alert.get("generator_url") or "")[:800],
        fingerprint=str(alert.get("fingerprint") or "")[:256],
        group_key=str(alert.get("groupKey") or alert.get("group_key") or "")[:512],
    )


def initial_symptom_from_evidence_batch(batch: list[dict[str, Any]]) -> InitialSymptom | None:
    """Best-effort InitialSymptom from omni evidence batch (canonical_query_snippet + alert_rule / alert_hint)."""
    if not batch:
        return None
    b0 = batch[0]
    snip = str(b0.get("canonical_query_snippet") or "").strip()
    ar = str(b0.get("alert_rule") or "").strip()
    hint = str(b0.get("alert_hint") or "").strip()
    if snip.startswith("{"):
        try:
            o = json.loads(snip)
            if isinstance(o, dict) and ("labels" in o or "annotations" in o):
                sym = initial_symptom_from_alertmanager_alert(o)
                upd: dict[str, Any] = {"canonical_query_snippet": snip[:2000]}
                if ar and not sym.alert_rule:
                    upd["alert_rule"] = ar[:240]
                if hint:
                    if not sym.error_hint:
                        upd["error_hint"] = hint[:500]
                    if not sym.summary:
                        upd["summary"] = hint[:500]
                if upd:
                    sym = sym.model_copy(update=upd)
                return sym
        except Exception:
            pass
    lbls = labels_dict_from_canonical_query_snippet(snip)
    if not lbls and not ar and not hint:
        return None
    return InitialSymptom(
        alertname=str(lbls.get("alertname") or lbls.get("alert_name") or ""),
        alert_rule=ar[:240],
        severity=str(lbls.get("severity") or ""),
        namespace=str(lbls.get("namespace") or "") or str(lbls.get("ns") or ""),
        labels=lbls,
        summary=hint[:500],
        error_hint=hint[:500],
        canonical_query_snippet=snip[:2000],
    )
