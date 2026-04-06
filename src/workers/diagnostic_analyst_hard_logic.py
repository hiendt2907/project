"""Quy tắc chẩn đoán cứng: SDK (kubelet state) là source of truth trước Prometheus."""

from __future__ import annotations

import json
import re
from typing import Any

# Khớp HighCPU / memory / % trong alert (đồng bộ tinh thần với diagnostic_resource).
_RE_WORKLOAD_RESOURCE_ALERT = re.compile(
    r"(highcpu|\bcpu\b|memory|\boom\b|oom|throttl|cgroup|millicore|millicores|rss|usage|\d+\s*%)",
    re.IGNORECASE,
)


def _alert_suggests_cpu_mem(alert_hint: str) -> bool:
    return bool(_RE_WORKLOAD_RESOURCE_ALERT.search(alert_hint or ""))


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


def _cpu_usage_effectively_zero(cpu_raw: str | None) -> bool:
    """PodMetrics trả cpu dạng '0', '0n', '1m', ... — coi ~0 nếu không đáng kể."""
    if cpu_raw is None:
        return True
    s = str(cpu_raw).strip().lower()
    if not s or s in ("0", "0n"):
        return True
    # millicores / nanocores
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mun]?)?$", s)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "n":  # nanocores — rất nhỏ
            return val < 1_000_000  # < 1m core
        if unit == "u":
            return val < 1000
        if unit == "m":
            return val < 10  # < 10m
        if unit == "":
            return val < 0.001
    return False


def _memory_usage_low_for_mem_alert(mem_raw: str | None, alert_mem: bool) -> bool:
    if not alert_mem or mem_raw is None:
        return False
    s = str(mem_raw).strip().lower()
    # "4528Ki", "4Mi"
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(ki|mi|gi)?$", s)
    if m:
        val = float(m.group(1))
        u = (m.group(2) or "ki").lower()
        bytes_approx = val * (1024 if u == "ki" else 1024**2 if u == "mi" else 1024**3)
        return bytes_approx < 20 * 1024 * 1024  # < 20Mi → coi là thấp cho lab
    return False


def apply_sdk_truth_hard_logic(evidence_by_probe: dict[str, dict[str, Any]]) -> str | None:
    """
    Nếu alert là CPU/mem workload nhưng SDK PodMetrics cho thấy CPU ~0 (và mem thấp nếu alert mem):
    kết luận FALSE_ALARM / STALE_METRIC — không gọi LLM.
    """
    alert_hint = ""
    for d in evidence_by_probe.values():
        alert_hint = str(d.get("alert_hint") or "")
        if alert_hint:
            break
    if not _alert_suggests_cpu_mem(alert_hint):
        return None

    met_ev = evidence_by_probe.get("k8s_clinical_pod_metrics")
    if not met_ev:
        return None
    res = str(met_ev.get("result") or "").upper()
    if res not in ("PASSED", "INCONCLUSIVE"):
        return None

    ef = _parse_extracted_fact(str(met_ev.get("extracted_fact") or ""))
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

    alert_mem = bool(re.search(r"\b(memory|oom|rss)\b", alert_hint, re.I))
    mem_low = (
        all(_memory_usage_low_for_mem_alert(m, alert_mem) for m in mem_vals)
        if mem_vals
        else False
    )
    extra = ""
    if alert_mem and mem_vals and mem_low:
        extra = " Memory usage from SDK is also modest."
    return (
        "FALSE_ALARM: SDK state machine confirms ~0% CPU usage (container metrics)."
        + extra
        + " STALE_METRIC: Ignoring Prometheus-backed alert until metrics align with live cluster state."
    )
