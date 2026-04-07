"""So sánh **tuyên bố của alert** với **state machine thật** từ K8s API (SDK probe).

- State machine (PodStatus / PodMetrics từ kubelet path) là **ground truth** — không “sai”.
- Alert (Prometheus / rule) có thể **false** (lag, series cũ, expr lệch).
- Không so → không kết luận được alert đúng/sai; module này chỉ trả lời khi có đủ evidence
  **và** tuyên bố workload CPU/mem **mâu thuẫn** rõ với SDK.

Không dùng shortcut trên free text nếu thiếu phân loại `symptom_group=workload_resource` từ dispatcher.
"""

from __future__ import annotations

import json
import re
from typing import Any

_BOOTSTRAP_WAITING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})


def _parse_extracted_fact(ef: str | None) -> dict[str, Any]:
    if not ef or not str(ef).strip():
        return {}
    s = str(ef).strip()
    if s.startswith("{"):
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def _parse_alert_labels_annotations_from_evidence(
    evidence_by_probe: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    for d in evidence_by_probe.values():
        snip = str(d.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            o = json.loads(snip)
        except Exception:
            continue
        L = o.get("labels")
        if isinstance(L, dict) and L:
            labels = L
            A = o.get("annotations")
            if isinstance(A, dict):
                annotations = A
            break
    return labels, annotations


def _signal_reason_from_container_signal(sig: str) -> str:
    if "=" not in sig:
        return ""
    return str(sig.split("=", 1)[-1]).strip()


def _waiting_reason_invalidates_cpu_stale_sample(reason: str) -> bool:
    r = reason.strip()
    if not r or r.lower() == "running":
        return False
    if r in _BOOTSTRAP_WAITING_REASONS:
        return False
    return True


def _sdk_invalidates_workload_cpu_stale_metric(stat_ev: dict[str, Any] | None) -> bool:
    """Pod không ở trạng thái mà so PodMetrics CPU với alert “nóng” là có nghĩa."""
    if not stat_ev:
        return False
    ef = _parse_extracted_fact(str(stat_ev.get("extracted_fact") or ""))
    ph = str(ef.get("phase") or "").strip()
    if ph in ("Failed", "Unknown", "Pending"):
        return True
    for sig in ef.get("container_signals") or []:
        if not isinstance(sig, str):
            continue
        r = _signal_reason_from_container_signal(sig)
        if _waiting_reason_invalidates_cpu_stale_sample(r):
            return True
    return False


def _batch_is_workload_resource_classified(
    evidence_by_probe: dict[str, dict[str, Any]],
) -> bool:
    return any(
        str(d.get("symptom_group") or "").strip() == "workload_resource"
        for d in evidence_by_probe.values()
    )


def _cpu_usage_effectively_zero(cpu_raw: str | None) -> bool:
    if cpu_raw is None:
        return True
    s = str(cpu_raw).strip().lower()
    if not s or s in ("0", "0n"):
        return True
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mun]?)?$", s)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "n":
            return val < 1_000_000
        if unit == "u":
            return val < 1000
        if unit == "m":
            return val < 10
        if unit == "":
            return val < 0.001
    return False


def _memory_usage_low_for_mem_alert(mem_raw: str | None, alert_mem: bool) -> bool:
    if not alert_mem or mem_raw is None:
        return False
    s = str(mem_raw).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(ki|mi|gi)?$", s)
    if m:
        val = float(m.group(1))
        u = (m.group(2) or "ki").lower()
        bytes_approx = val * (1024 if u == "ki" else 1024**2 if u == "mi" else 1024**3)
        return bytes_approx < 20 * 1024 * 1024
    return False


def compare_alert_claim_to_sdk_state(
    evidence_by_probe: dict[str, dict[str, Any]],
) -> str | None:
    """
    Nếu alert (workload CPU/mem) **mâu thuẫn** PodMetrics SDK (~0 CPU trong phạm vi so được):
    trả về chẩn đoán ngắn — **alert sai / không khớp state machine**, không phủ nhận SDK.

    Trả None khi không đủ dữ liệu hoặc không mâu thuẫn rõ.
    """
    if not _batch_is_workload_resource_classified(evidence_by_probe):
        return None

    labels, _ann = _parse_alert_labels_annotations_from_evidence(evidence_by_probe)
    if str(labels.get("reason") or "").strip():
        return None

    stat_ev = evidence_by_probe.get("k8s_clinical_pod_status")
    if _sdk_invalidates_workload_cpu_stale_metric(stat_ev):
        return None

    met_ev = evidence_by_probe.get("k8s_clinical_pod_metrics")
    if not met_ev:
        return None
    res = str(met_ev.get("result") or "").upper()
    if res not in ("PASSED", "INCONCLUSIVE"):
        return None

    ef = _parse_extracted_fact(str(met_ev.get("extracted_fact") or ""))
    if res == "INCONCLUSIVE":
        omit = str(ef.get("omit_reason") or "")
        if "404" in omit or "not_found" in omit.lower() or "podmetrics" in omit.lower():
            return None
        if not ef.get("containers"):
            return None

    containers = ef.get("containers")
    cpu_vals: list[str] = []
    mem_vals: list[str] = []
    if isinstance(containers, list) and containers:
        for c in containers:
            if not isinstance(c, dict):
                continue
            cpu_vals.append(str(c.get("cpu") or "0"))
            mem_vals.append(str(c.get("memory") or ""))
    if not cpu_vals:
        raw = str(met_ev.get("raw") or "")
        m = re.search(r"cpu=([^\s]+)", raw, re.I)
        cpu_vals = [m.group(1) if m else "0"]

    cpu_zero = all(_cpu_usage_effectively_zero(c) for c in cpu_vals)
    if not cpu_zero:
        return None

    alert_hint = ""
    for d in evidence_by_probe.values():
        alert_hint = str(d.get("alert_hint") or "")
        if alert_hint:
            break
    hint_for_mem = alert_hint
    if labels:
        hint_for_mem = json.dumps({"labels": labels, "annotations": _ann}, ensure_ascii=False)[:1200]
    alert_mem = bool(re.search(r"\b(memory|oom|rss)\b", hint_for_mem, re.I))
    mem_low = (
        all(_memory_usage_low_for_mem_alert(m, alert_mem) for m in mem_vals)
        if mem_vals
        else False
    )
    extra = ""
    if alert_mem and mem_vals and mem_low:
        extra = " Live memory from API is also low for this scope."

    return (
        "Alert claims elevated workload CPU; Kubernetes API state machine (PodMetrics) shows "
        "negligible CPU for containers in scope."
        + extra
        + " Conclusion: the firing alert is inconsistent with live cluster state "
        "(stale or mismatched Prometheus series vs kubelet)."
    )
