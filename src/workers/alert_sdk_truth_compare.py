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
    # Proof-of-fault: an OOMKilled / crash-looping pod is a CONFIRMED fault, not a
    # stale-metric false alarm — even if sampled momentarily Running between restarts.
    # The contrast path must yield so the pipeline proceeds to remediation/advisory.
    if bool(ef.get("has_oom_killed")) or bool(ef.get("has_crash_loop")):
        return True
    for pod in ef.get("pods") or []:
        if isinstance(pod, dict) and (pod.get("has_oom_killed") or pod.get("has_crash_loop")):
            return True
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

    alert_hint = ""
    for d in evidence_by_probe.values():
        alert_hint = str(d.get("alert_hint") or "")
        if alert_hint:
            break
    hint_for_mem = alert_hint
    if labels:
        # Keep the free-text alert_hint too — labels JSON alone may bury the metric
        # dimension inside a CamelCase alertname (e.g. PodMemoryWorkingSetVsLimitHigh)
        # where a strict \bmemory\b boundary would miss it.
        hint_for_mem = (
            alert_hint
            + " "
            + json.dumps({"labels": labels, "annotations": _ann}, ensure_ascii=False)[:1200]
        )
    alert_mem = bool(re.search(r"(memory|oom|rss|workingset|working_set|mem_)", hint_for_mem, re.I))
    mem_low = (
        all(_memory_usage_low_for_mem_alert(m, alert_mem) for m in mem_vals)
        if mem_vals
        else False
    )

    # Dimension-aware contrast gate: a MEMORY alert must be contradicted by LOW live
    # memory — NOT by idle CPU. Suppressing a memory alert purely because CPU is zero
    # would dismiss a genuine high-memory / OOM-risk pod, since CPU and memory are
    # orthogonal resources. Only emit the contrast on the dimension the alert claims.
    if alert_mem:
        if not (mem_vals and mem_low):
            return None
        return (
            "Alert báo bộ nhớ workload cao (working set vs limit); nhưng state machine Kubernetes "
            "(PodMetrics) cho thấy bộ nhớ thực tế của các container trong phạm vi đang THẤP. Tin vào "
            "state machine cho snapshot này; coi alert đang kích hoạt là ĐÁNG NGHI (báo động giả / series "
            "cũ hoặc lệch, sai selector, hoặc trễ scrape–recording) cho tới khi xác minh lại trên Prometheus "
            "và rule cảnh báo."
            + (" CPU thực tế từ API cũng không đáng kể trong phạm vi này." if cpu_zero else "")
        )

    if not cpu_zero:
        return None
    return (
        "Alert báo CPU workload cao; nhưng state machine Kubernetes (PodMetrics) cho thấy CPU thực tế của "
        "các container trong phạm vi không đáng kể. Tin vào state machine cho snapshot này; coi alert đang "
        "kích hoạt là ĐÁNG NGHI (báo động giả / series cũ hoặc lệch, sai selector, hoặc trễ scrape–recording) "
        "cho tới khi xác minh lại trên Prometheus và rule cảnh báo."
    )


def _first_alert_rule(evidence_by_probe: dict[str, dict[str, Any]]) -> str:
    for d in evidence_by_probe.values():
        r = str(d.get("alert_rule") or "").strip()
        if r:
            return r
    return ""


def _pod_phase_from_status_probe(stat_ev: dict[str, Any] | None) -> str:
    if not stat_ev:
        return ""
    ef = _parse_extracted_fact(str(stat_ev.get("extracted_fact") or ""))
    pods = ef.get("pods")
    if isinstance(pods, list) and pods and isinstance(pods[0], dict):
        return str(pods[0].get("phase") or "").strip()
    return ""


def _metrics_api_container_lines(met_ev: dict[str, Any] | None) -> str:
    if not met_ev:
        return ""
    ef = _parse_extracted_fact(str(met_ev.get("extracted_fact") or ""))
    lines: list[str] = []
    for c in (ef.get("containers") or [])[:6]:
        if not isinstance(c, dict):
            continue
        lines.append(
            f"  - container {c.get('name', '?')}: cpu={c.get('cpu', '?')} memory={c.get('memory', '?')}"
        )
    return "\n".join(lines) if lines else ""


def _prom_cpu_workload_line(prom_ev: dict[str, Any] | None) -> str:
    if not prom_ev:
        return ""
    ef = _parse_extracted_fact(str(prom_ev.get("extracted_fact") or ""))
    if not ef:
        return ""
    s0 = ef.get("s0")
    unit = str(ef.get("unit") or "").strip()
    if s0 is None and not unit:
        return ""
    raw = str(prom_ev.get("raw") or "").strip()
    raw_short = raw[:220] + ("…" if len(raw) > 220 else "")
    return (
        f"  - prom_pod_cpu_cores (workload-scoped PromQL sum): s0={s0} unit={unit or 'n/a'}\n"
        f"    raw excerpt: {raw_short if raw_short else '(empty)'}"
    )


def _contrast_operator_sections_en(
    *,
    ns: str,
    dep: str,
    pod: str,
    alertname: str,
    container: str,
    summ: str,
    desc_short: str,
    hint_short: str,
    phase: str,
    metrics_lines: str,
    prom_line: str,
    rule: str,
    contrast_narrative: str,
    trace_id: str,
) -> list[str]:
    why = (
        "Operational default on this contrast path: trust the state machine (kubelet / Metrics API in scope). "
        "The firing alert is suspect — typical causes: (1) PromQL or recording rule matching an old pod/replica; "
        "(2) alert labels not aligned with kube_pod_* / workload selectors; (3) scrape or recording lag vs this snapshot. "
        "Verify the rule and series on Prometheus; do not assume the alert is true without that cross-check."
    )
    return [
        "=== STATE_MACHINE_CONTRAST (read-only; no mutate) ===",
        "",
        "WHO / WHERE (scope)",
        f"- Kubernetes namespace: {ns}",
        f"- Workload deployment (from alert labels): {dep}",
        f"- Pod name on the alert: {pod}",
        f"- Container (if labeled): {container or '(not in labels)'}",
        "",
        "WHAT THE ALERT CLAIMS",
        f"- alertname: {alertname}",
        f"- ingress/rule hint: {rule or '(probe did not carry alert_rule)'}",
        f"- annotation summary: {summ or '(empty)'}",
        f"- annotation description (trimmed): {desc_short or '(empty)'}",
        f"- alert_hint (from evidence pipeline): {hint_short or '(empty)'}",
        "",
        "WHAT STATE MACHINE SHOWS (same trace; kubelet / Metrics API)",
        f"- Pod phase (k8s_clinical_pod_status): {phase or '(unparsed)'}",
        "- Metrics API container view (k8s_clinical_pod_metrics):",
        metrics_lines or "  (no container rows parsed)",
        "- Prometheus workload-scoped CPU probe (if present):",
        prom_line or "  (no prom_pod_cpu_cores row)",
        "",
        "WHY ALERT AND STATE MACHINE DIVERGE (alert suspect until verified)",
        why,
        "",
        "DO FIRST (commands)",
        f"  kubectl -n {ns} get pod {pod} -o wide",
        f"  kubectl -n {ns} describe pod {pod} | sed -n '1,120p'",
        f"  kubectl -n {ns} top pod {pod} 2>/dev/null || true",
        "  Open Prometheus → paste the firing rule expression → verify pod= and namespace= match this pod name and generation.",
        "",
        "SUGGESTED TOOL (pipeline): verify_metrics_alignment",
        "",
        f"TRACE: {trace_id}",
        "",
        "--- Narrative (same as omni-actions diagnosis) ---",
        contrast_narrative.strip(),
    ]


def _contrast_operator_sections_vi(
    *,
    ns: str,
    dep: str,
    pod: str,
    alertname: str,
    container: str,
    summ: str,
    desc_short: str,
    hint_short: str,
    phase: str,
    metrics_lines: str,
    prom_line: str,
    rule: str,
    contrast_narrative: str,
    trace_id: str,
) -> list[str]:
    why_vi = (
        "Mặc định vận hành trên luồng contrast: **tin state machine** (kubelet / Metrics API trong phạm vi). "
        "Alert đang firing là **đáng nghi** — hay gặp: (1) PromQL/recording khớp pod/replica cũ; "
        "(2) label alert lệch so với kube_pod_* / selector workload; (3) scrape–recording trễ so snapshot này. "
        "Đối soát rule và series trên Prometheus; không coi alert là đúng nếu chưa kiểm tra."
    )
    return [
        "=== STATE_MACHINE_CONTRAST (chỉ đọc; không mutate) ===",
        "",
        "PHẠM VI / VỊ TRÍ",
        f"- Namespace Kubernetes: {ns}",
        f"- Deployment workload (từ label alert): {dep}",
        f"- Tên pod trên alert: {pod}",
        f"- Container (nếu có label): {container or '(không có trong labels)'}",
        "",
        "ALERT ĐANG TUYÊN BỐ GÌ",
        f"- alertname: {alertname}",
        f"- gợi ý ingress/rule: {rule or '(probe không mang alert_rule)'}",
        f"- annotation summary: {summ or '(trống)'}",
        f"- annotation description (rút gọn): {desc_short or '(trống)'}",
        f"- alert_hint (pipeline evidence): {hint_short or '(trống)'}",
        "",
        "STATE MACHINE CHO THẤY (cùng trace; kubelet / Metrics API)",
        f"- Phase pod (k8s_clinical_pod_status): {phase or '(chưa parse)'}",
        "- Metrics API theo container (k8s_clinical_pod_metrics):",
        metrics_lines or "  (chưa parse được dòng container)",
        "- Probe CPU Prometheus theo workload (nếu có):",
        prom_line or "  (không có prom_pod_cpu_cores)",
        "",
        "VÌ SAO ALERT VÀ STATE MACHINE LỆCH (alert đáng nghi tới khi verify)",
        why_vi,
        "",
        "LÀM TRƯỚC (lệnh)",
        f"  kubectl -n {ns} get pod {pod} -o wide",
        f"  kubectl -n {ns} describe pod {pod} | sed -n '1,120p'",
        f"  kubectl -n {ns} top pod {pod} 2>/dev/null || true",
        "  Mở Prometheus → dán biểu thức rule đang firing → kiểm tra pod= và namespace= khớp pod/generation này.",
        "",
        "GỢI Ý TOOL (pipeline): verify_metrics_alignment",
        "",
        f"TRACE: {trace_id}",
        "",
        "--- Diễn giải (giống diagnosis omni-actions) ---",
        contrast_narrative.strip(),
    ]


def build_contrast_operator_telegram_body(
    evidence_by_probe: dict[str, dict[str, Any]],
    contrast_narrative: str,
    trace_id: str,
    *,
    locale: str = "both",
) -> str:
    """Plain-text operator digest: who/where, what alert vs SDK/Prom, why mismatch, first actions.

    Contrast path is deterministic (no LLM); this packs structured fields from probes so humans
    are not left with only the generic narrative paragraph.
    """
    labels, annotations = _parse_alert_labels_annotations_from_evidence(evidence_by_probe)
    ns = str(labels.get("namespace") or "").strip() or "unknown"
    dep = str(labels.get("deployment") or labels.get("workload") or "").strip() or "unknown"
    pod = str(labels.get("pod") or "").strip() or "unknown"
    alertname = str(labels.get("alertname") or "").strip() or "unknown"
    container = str(labels.get("container") or "").strip() or ""
    summ = str((annotations or {}).get("summary") or "").strip()
    desc = str((annotations or {}).get("description") or "").strip()
    desc_short = (desc[:280] + "…") if len(desc) > 280 else desc

    alert_hint = ""
    for d in evidence_by_probe.values():
        h = str(d.get("alert_hint") or "").strip()
        if h:
            alert_hint = h
            break
    hint_short = (alert_hint[:320] + "…") if len(alert_hint) > 320 else alert_hint

    stat_ev = evidence_by_probe.get("k8s_clinical_pod_status")
    if not isinstance(stat_ev, dict):
        stat_ev = None
    met_ev = evidence_by_probe.get("k8s_clinical_pod_metrics")
    if not isinstance(met_ev, dict):
        met_ev = None
    prom_ev = evidence_by_probe.get("prom_pod_cpu_cores")
    if not isinstance(prom_ev, dict):
        prom_ev = None

    phase = _pod_phase_from_status_probe(stat_ev)
    metrics_lines = _metrics_api_container_lines(met_ev)
    prom_line = _prom_cpu_workload_line(prom_ev)
    rule = _first_alert_rule(evidence_by_probe)
    narr = contrast_narrative.strip()
    loc = str(locale or "both").strip().lower()
    if loc not in ("en", "vi", "both"):
        loc = "both"
    kw = dict(
        ns=ns,
        dep=dep,
        pod=pod,
        alertname=alertname,
        container=container,
        summ=summ,
        desc_short=desc_short,
        hint_short=hint_short,
        phase=phase,
        metrics_lines=metrics_lines,
        prom_line=prom_line,
        rule=rule,
        contrast_narrative=narr,
        trace_id=trace_id,
    )
    if loc == "en":
        parts = [_contrast_operator_sections_en(**kw)]
    elif loc == "vi":
        parts = [_contrast_operator_sections_vi(**kw)]
    else:
        parts = [
            _contrast_operator_sections_en(**kw),
            ["", "----------", "[VI]", ""],
            _contrast_operator_sections_vi(**kw),
        ]
    flat: list[str] = []
    for p in parts:
        flat.extend(p)
    body = "\n".join(flat)
    return body[:3900]


def build_contrast_diagnosis_for_action(
    evidence_by_probe: dict[str, dict[str, Any]],
    contrast_narrative: str,
    max_len: int = 1800,
) -> str:
    """Shorter diagnosis string for SUGGEST_REMEDIATION / Kafka (still names workload + mechanism)."""
    labels, _ann = _parse_alert_labels_annotations_from_evidence(evidence_by_probe)
    ns = str(labels.get("namespace") or "").strip() or "unknown"
    dep = str(labels.get("deployment") or labels.get("workload") or "").strip() or "unknown"
    pod = str(labels.get("pod") or "").strip() or "unknown"
    alertname = str(labels.get("alertname") or "").strip() or "unknown"
    _dim_hint = json.dumps({"labels": labels, "annotations": _ann}, ensure_ascii=False)
    _is_mem = bool(re.search(r"\b(memory|oom|rss)\b", _dim_hint + " " + alertname, re.I))
    _metric_vi = "bộ nhớ (memory)" if _is_mem else "CPU"
    head = (
        f"[phạm vi đối chiếu ns={ns} deploy={dep} pod={pod} alertname={alertname}] "
        f"Tin vào state machine: PodMetrics cho thấy {_metric_vi} thực tế không đáng kể so với mức "
        f"mà alert tuyên bố — alert đang kích hoạt bị nghi ngờ (báo động giả / series cũ hoặc lệch) "
        f"cho tới khi xác minh lại Prometheus/rule."
    )
    tail = contrast_narrative.strip()
    out = f"{head}\n\n{tail}"
    return out if len(out) <= max_len else (out[: max_len - 1] + "…")
