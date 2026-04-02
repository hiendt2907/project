"""Gom tham số query Prometheus qua nhiều tin — đủ pod/workload mới chạy tool."""

from __future__ import annotations

import re
from typing import Any

from workers.clarification import (
    _BAD_POD_NAME_TOKENS,
    _RE_K8S_NAME,
    _RE_POD_NAME,
    parse_namespace_hint,
)
from workers.promql_presets import resolve_intent_from_keywords

# Token đơn không coi là tên pod (tránh "cpu"/"host" thành pod)
_SINGLE_TOKEN_SKIP = frozenset(
    {
        "cpu",
        "ram",
        "yes",
        "no",
        "ok",
        "host",
        "node",
        "pod",
        "ns",
        "namespace",
    }
)


def extract_vm_slots_from_text(text: str) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    raw = (text or "").strip()
    if not raw:
        return slots
    tl = raw.lower()
    # Chỉ ghi intent khi user nhắc métric — tránh "redis" → default cpu đè intent cũ.
    if any(
        k in tl
        for k in (
            "cpu",
            "ram",
            "memory",
            "disk",
            "network",
            "bộ nhớ",
            "đĩa",
            "mạng",
            "vcpu",
            "iops",
        )
    ):
        slots["intent"] = resolve_intent_from_keywords(raw)
    ns = parse_namespace_hint(raw)
    if ns:
        slots["namespace"] = ns
    km = _RE_K8S_NAME.search(raw)
    if km:
        slots["pod_name"] = km.group(0)
    m = _RE_POD_NAME.search(raw)
    if m:
        name = m.group(1).strip()
        if name.lower() not in _BAD_POD_NAME_TOKENS and len(name) >= 2:
            slots["pod_name"] = name
    dm = re.search(r"\b(\d+)\s*(h|m)\b", raw.lower())
    if dm:
        slots["duration"] = f"{dm.group(1)}{dm.group(2)}"
    if "pod_name" not in slots:
        st = re.match(r"^\s*([\w.-]+)\s*$", raw.strip())
        if st:
            tok = st.group(1).strip()
            if (
                len(tok) >= 2
                and tok.lower() not in _BAD_POD_NAME_TOKENS
                and tok.lower() not in _SINGLE_TOKEN_SKIP
            ):
                slots["pod_name"] = tok
    return slots


def enrich_slots_from_discovery(
    slots: dict[str, Any],
    discovery: list[dict[str, str]] | None,
) -> dict[str, Any]:
    """Tra cứu namespace từ kết quả list_all trước đó khi user chỉ nói tên pod/workload."""
    if not discovery:
        return slots
    out = dict(slots)
    ns = str(out.get("namespace") or "").strip()
    pod = str(out.get("pod_name") or "").strip()
    if ns and pod:
        return out
    if not pod:
        return out
    hint = pod.lower().strip()
    for row in discovery:
        name = (row.get("name") or "").strip()
        pns = (row.get("namespace") or "").strip()
        if not name or not pns:
            continue
        nl = name.lower()
        if nl == hint or nl.startswith(hint + "-") or hint in nl:
            out["namespace"] = pns
            out["pod_name"] = name
            break
    return out


def merge_vm_slots(existing: dict[str, Any] | None, text: str) -> dict[str, Any]:
    base = dict(existing or {})
    new = extract_vm_slots_from_text(text)
    for k, v in new.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        base[k] = v
    return base


def vm_slots_ready(slots: dict[str, Any]) -> bool:
    """Pod: intent+ns+pod. Host: intent đủ (target_type=host)."""
    intent = str(slots.get("intent") or "").strip()
    tt = str(slots.get("target_type") or "").strip().lower()
    if tt == "host":
        return bool(intent)
    pod = str(slots.get("pod_name") or "").strip()
    ns = str(slots.get("namespace") or "").strip()
    return bool(intent and pod and ns)


def followup_indicates_host(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False
    return any(
        k in t
        for k in (
            "host",
            "node",
            "máy chủ",
            "localhost",
            "psutil",
            "máy host",
            "toàn bộ host",
            "worker node",
        )
    )


def nudge_vm_slots_message(slots: dict[str, Any]) -> str:
    lines = [
        "Em đang gom thông tin — đại ca có thể nhắn rải vài tin (không cần một lần đủ).",
    ]
    tt = str(slots.get("target_type") or "").strip().lower()
    missing: list[str] = []
    if not str(slots.get("intent") or "").strip():
        missing.append("loại métric (CPU/RAM/…)")
    if tt == "host":
        if missing:
            lines.append("Còn thiếu: " + ", ".join(missing) + ".")
        if str(slots.get("intent") or "").strip():
            lines.append(f"Đã có intent: `{slots['intent']}` — mục tiêu **Host**.")
        lines.append("Gõ `host` hoặc `node` nếu muốn métric máy; hoặc gõ pod+namespace để chuyển sang Pod.")
        return "\n".join(lines)
    if not str(slots.get("namespace") or "").strip():
        missing.append("namespace")
    if not str(slots.get("pod_name") or "").strip():
        missing.append("pod/workload (tên từ user hoặc sau `resolve_pod_identity` / `k8s_list_pods` / `list_all_pods_sdk`)")
    if missing:
        lines.append("Còn thiếu: " + ", ".join(missing) + ".")
    if str(slots.get("namespace") or "").strip():
        lines.append(f"Đã có namespace: `{slots['namespace']}`.")
    if str(slots.get("intent") or "").strip():
        lines.append(f"Đã có intent: `{slots['intent']}`.")
    if str(slots.get("pod_name") or "").strip():
        lines.append(f"Đã có pod/workload: `{slots['pod_name']}`.")
    lines.append("Nếu muốn métric theo **máy host** thì gõ `host` hoặc `node`.")
    lines.append("Em **chưa** query VM cho tới khi đủ intent + namespace + pod (không đoán mặc định).")
    return "\n".join(lines)


def vm_slots_to_tool_args(slots: dict[str, Any], _ctx: Any) -> dict[str, Any]:
    ns = str(slots.get("namespace") or "").strip()
    pod = str(slots.get("pod_name") or "").strip()
    intent = str(slots.get("intent") or "").strip()
    tt = str(slots.get("target_type") or "").strip().lower()
    if tt == "host":
        if not intent:
            raise ValueError("vm_slots_to_tool_args: thiếu intent (host)")
        return {
            "intent": intent,
            "target_type": "host",
            "duration": str(slots.get("duration") or "1h").strip() or "1h",
        }
    if not ns or not pod or not intent:
        raise ValueError("vm_slots_to_tool_args: thiếu intent/namespace/pod_name từ slot")
    return {
        "intent": intent,
        "pod_name": pod,
        "namespace": ns,
        "target_type": "pod",
        "duration": str(slots.get("duration") or "1h").strip() or "1h",
    }
