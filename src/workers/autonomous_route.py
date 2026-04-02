"""
Định tuyến tự động (SDK) trước LLM: kubectl-style → tool thật, không hỏi user chọn.
Chỉ dùng kubernetes_asyncio / metrics.k8s.io — không shell.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_RE_KUBECTL_TOP = re.compile(r"kubectl\s+top\s+pods?\b", re.I)
_RE_KUBECTL_GET_PO = re.compile(r"kubectl\s+get\s+pods?\b", re.I)
_RE_KUBECTL_LOGS = re.compile(r"kubectl\s+logs\b", re.I)
_RE_KUBECTL_DESCRIBE = re.compile(r"kubectl\s+describe\s+pod\b", re.I)
_RE_NS_FLAG = re.compile(r"(?:-n|--namespace)\s+([\w.-]+)", re.I)
_RE_PODISH = re.compile(r"\b([\w][\w.-]{2,200}(?:-[\w]{3,12}){1,4})\b")


def _ns_from_text(text: str, default_ns: str) -> str:
    m = _RE_NS_FLAG.search(text or "")
    if m:
        return m.group(1).strip()
    return (default_ns or "multi-agent").strip()


def _parse_tail_hint(text: str) -> int:
    t = (text or "").lower()
    m = re.search(r"(?:tail|lines|--tail)\s*[=:]?\s*(\d{1,3})", t)
    if m:
        return max(1, min(int(m.group(1)), 500))
    if "full" in t or "nhiều" in t or "dài" in t:
        return 200
    return 120


def _parse_kubectl_logs(text: str) -> tuple[str | None, str | None]:
    s = (text or "").strip()
    # kubectl logs POD -n NS  [flags]
    m = re.match(
        r"kubectl\s+logs\s+(?:-f\s+)?(\S+)\s+(?:-n|--namespace)\s+(\S+)",
        s,
        re.I,
    )
    if m:
        return m.group(1), m.group(2)
    # kubectl logs -n NS POD
    m = re.match(
        r"kubectl\s+logs\s+(?:-f\s+)?(?:-n|--namespace)\s+(\S+)\s+(\S+)",
        s,
        re.I,
    )
    if m:
        return m.group(2), m.group(1)
    return None, None


def _parse_kubectl_describe(text: str) -> tuple[str | None, str | None]:
    s = (text or "").strip()
    m = re.match(
        r"kubectl\s+describe\s+pod\s+(\S+)\s+(?:-n|--namespace)\s+(\S+)",
        s,
        re.I,
    )
    if m:
        return m.group(1), m.group(2)
    m = re.match(
        r"kubectl\s+describe\s+pod\s+(?:-n|--namespace)\s+(\S+)\s+(\S+)",
        s,
        re.I,
    )
    if m:
        return m.group(2), m.group(1)
    return None, None


def _guess_pod_token(text: str) -> str | None:
    """Lấy token giống tên pod (hash-hash) từ câu tiếng Việt / tự nhiên."""
    raw = text or ""
    for m in _RE_PODISH.finditer(raw):
        tok = m.group(1)
        if tok.count("-") >= 2 and len(tok) >= 12:
            return tok
    return None


def _vietnamese_logs_intent(text: str, default_ns: str) -> tuple[str, str, int] | None:
    tl = (text or "").lower()
    # Dùng word boundary cho "log"/"tail" — tránh kích hoạt nhầm vì "topology" chứa "log".
    has_logs_intent = bool(
        re.search(r"\blogs?\b", tl)
        or re.search(r"\btail\b", tl)
        or "nhật ký" in tl
        or "nhat ky" in tl
        or "xem log" in tl
        or "stream log" in tl
    )
    if not has_logs_intent:
        return None
    pod = _guess_pod_token(text)
    if not pod:
        return None
    ns = _ns_from_text(text, default_ns)
    tail = _parse_tail_hint(text)
    return pod, ns, tail


async def try_autonomous_sdk_route(ctx: Any, raw_user_text: str) -> str | None:
    """
    Trả về kết quả tool nếu khớp pattern kubectl / xem logs — không gọi LLM.
    """
    t = (raw_user_text or "").strip()
    if len(t) < 3:
        return None
    default_ns = getattr(getattr(ctx, "settings", None), "k8s_default_namespace", None) or "multi-agent"

    from workers.k8s_tools import (
        tool_inspect_pod_deep,
        tool_list_namespace_pods,
        tool_namespace_pods_top,
    )

    if _RE_KUBECTL_TOP.search(t):
        ns = _ns_from_text(t, default_ns)
        logger.info("autonomous_route: namespace_pods_top ns=%s", ns)
        return await tool_namespace_pods_top(ctx, {"namespace": ns})

    if _RE_KUBECTL_GET_PO.search(t) and _RE_NS_FLAG.search(t):
        ns = _ns_from_text(t, default_ns)
        logger.info("autonomous_route: list_namespace_pods ns=%s", ns)
        return await tool_list_namespace_pods(ctx, {"namespace": ns})

    if _RE_KUBECTL_LOGS.search(t):
        pod, ns = _parse_kubectl_logs(t)
        if pod and ns:
            tail = _parse_tail_hint(t)
            logger.info("autonomous_route: inspect_pod_deep (kubectl logs) pod=%s ns=%s", pod, ns)
            return await tool_inspect_pod_deep(
                ctx,
                {"pod_name": pod, "namespace": ns, "tail_lines": tail},
            )

    if _RE_KUBECTL_DESCRIBE.search(t):
        pod, ns = _parse_kubectl_describe(t)
        if pod and ns:
            logger.info("autonomous_route: inspect_pod_deep (describe) pod=%s ns=%s", pod, ns)
            return await tool_inspect_pod_deep(ctx, {"pod_name": pod, "namespace": ns, "tail_lines": 15})

    vn = _vietnamese_logs_intent(t, default_ns)
    if vn:
        pod, ns, tail = vn
        logger.info("autonomous_route: inspect_pod_deep (vi logs) pod=%s ns=%s tail=%s", pod, ns, tail)
        return await tool_inspect_pod_deep(
            ctx,
            {"pod_name": pod, "namespace": ns, "tail_lines": tail},
        )

    if re.search(r"kubectl\s+(get\s+nodes?|describe\s+nodes?)", t, re.I) or "xem node" in t.lower():
        from workers.k8s_readonly_tools import tool_k8s_list_nodes
        logger.info("autonomous_route: k8s_list_nodes")
        return await tool_k8s_list_nodes(ctx, {"force_refresh": False, "node_name": ""})

    if re.search(r"kubectl\s+get\s+(svc|services|ingress|ingresses)", t, re.I):
        from workers.k8s_readonly_tools import tool_k8s_list_services, tool_k8s_list_ingress
        ns = _ns_from_text(t, "")
        if "ingress" in t.lower() or "ing" in t.lower() and "ping" not in t.lower():
            logger.info("autonomous_route: k8s_list_ingress ns=%s", ns)
            return await tool_k8s_list_ingress(ctx, {"force_refresh": False, "namespace": ns})
        else:
            logger.info("autonomous_route: k8s_list_services ns=%s", ns)
            return await tool_k8s_list_services(ctx, {"force_refresh": False, "namespace": ns})

    return None
