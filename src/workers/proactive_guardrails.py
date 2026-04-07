"""Granular freeze + resource lease for proactive path (Redis)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)

# Tools that mutate cluster state — require lease before execution in proactive fallback.
PROACTIVE_MUTATE_TOOLS: frozenset[str] = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_patch_resource",
        "kubectl_cluster",
    }
)


_POD_FQN_TAIL = re.compile(r"-[a-f0-9]{9,10}-[a-z0-9]{5}$", re.IGNORECASE)
_RS_TAIL = re.compile(r"-[a-f0-9]{9,10}$", re.IGNORECASE)


def looks_like_kubernetes_pod_full_name(name: str) -> bool:
    """Pod name …-replicasethash-podhash (tránh đưa làm Deployment)."""
    s = (name or "").strip()
    if len(s) < 18:
        return False
    return bool(_POD_FQN_TAIL.search(s))


def looks_like_kubernetes_replicaset_name(name: str) -> bool:
    """ReplicaSet …-deployment-hash (không phải tên Deployment)."""
    s = (name or "").strip()
    if len(s) < 12:
        return False
    if looks_like_kubernetes_pod_full_name(s):
        return False
    return bool(_RS_TAIL.search(s)) and len(s) > 24


def proactive_rollout_restart_allowed(ev: "AnomalyEvent", args: dict[str, Any]) -> tuple[bool, str]:
    """Bắt buộc namespace + deployment; từ chối hint giống Pod/ReplicaSet."""
    ns = str((args or {}).get("namespace") or getattr(ev, "namespace", "") or "").strip()
    dep = str((args or {}).get("deployment") or (args or {}).get("name") or "").strip()
    if not ns:
        return False, "missing_namespace"
    if not dep:
        return False, "missing_deployment"
    if looks_like_kubernetes_pod_full_name(dep):
        return False, "hint_looks_like_pod_name"
    if looks_like_kubernetes_replicaset_name(dep):
        return False, "hint_looks_like_replicaset_name"
    return True, ""


def proactive_gigo_cluster_identity_ok(ev: "AnomalyEvent") -> tuple[bool, str]:
    """GIGO ingress: cần ít nhất một trong hai — scope K8s (namespace) hoặc ngữ cảnh PromQL (trigger).

    Evidence path có ``coerce_evidence_dict``; proactive Kafka trước đây không có lọc tương đương — input rác → ReAct rác.
    Luồng ``evaluate_proactive_triggers`` luôn set ``trigger_promql``; stub chỉ ``canonical_query`` thì fail sớm tại đây.
    """
    if (ev.namespace or "").strip():
        return True, ""
    if (ev.trigger_promql or "").strip():
        return True, ""
    return False, "gigo_missing_namespace_and_trigger_promql"


def _sanitize_segment(s: str, max_len: int = 120) -> str:
    t = re.sub(r"[^a-zA-Z0-9._-]", "_", (s or "").strip())[:max_len]
    return t or "_"


def resource_freeze_redis_key(prefix: str, namespace: str, kind: str, name: str) -> str:
    """Stable key for SET; includes hash to avoid Redis key length issues."""
    raw = f"{namespace}|{kind}|{name}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{_sanitize_segment(namespace, 253)}:{_sanitize_segment(kind)}:{_sanitize_segment(name)}:{h}"


def namespace_fallback_freeze_key(prefix: str, namespace: str) -> str:
    return f"{prefix}:ns:{_sanitize_segment(namespace, 253)}"


def extract_resource_ref(tool_name: str, args: dict[str, Any]) -> tuple[str, str, str] | None:
    """Derive (namespace, kind, name) for K8s objects from tool args. None if not a single resource."""
    if not args:
        return None
    ns = str(args.get("namespace") or "").strip()
    if tool_name == "k8s_rollout_restart":
        dep = str(args.get("deployment") or args.get("name") or "").strip()
        if ns and dep:
            return (ns, "Deployment", dep)
        return None
    if tool_name in ("inspect_pod_deep", "tool_inspect_pod_deep", "k8s_inspect_pod_deep"):
        pod = str(args.get("pod_name") or args.get("name") or "").strip()
        if ns and pod:
            return (ns, "Pod", pod)
        return None
    if tool_name == "k8s_scale_deployment":
        dep = str(args.get("deployment") or args.get("name") or "").strip()
        if ns and dep:
            return (ns, "Deployment", dep)
        return None
    if tool_name == "k8s_patch_resource":
        rt = str(args.get("resource_type") or "Deployment").strip()
        name = str(args.get("name") or "").strip()
        if ns and name:
            return (ns, rt, name)
        return None
    return None


async def is_resource_frozen(
    r: Any,
    *,
    key_prefix: str,
    namespace: str,
    kind: str,
    name: str,
) -> bool:
    key = resource_freeze_redis_key(key_prefix, namespace, kind, name)
    v = await r.get(key)
    return v is not None and str(v).strip() != ""


async def is_namespace_frozen_fallback(
    r: Any,
    *,
    key_prefix: str,
    namespace: str,
) -> bool:
    key = namespace_fallback_freeze_key(key_prefix, namespace)
    v = await r.get(key)
    return v is not None and str(v).strip() != ""


async def set_resource_freeze(
    r: Any,
    *,
    key_prefix: str,
    namespace: str,
    kind: str,
    name: str,
    ttl_sec: int,
    trace_id: str,
    reason: str,
) -> str:
    key = resource_freeze_redis_key(key_prefix, namespace, kind, name)
    payload = {
        "trace_id": trace_id,
        "set_at": int(__import__("time").time()),
        "reason": reason[:2000],
        "namespace": namespace,
        "kind": kind,
        "name": name,
    }
    await r.setex(key, ttl_sec, json.dumps(payload, ensure_ascii=False))
    logger.info("proactive resource freeze set key=%s trace=%s", key, trace_id)
    return key


async def set_namespace_freeze_fallback(
    r: Any,
    *,
    key_prefix: str,
    namespace: str,
    ttl_sec: int,
    trace_id: str,
    reason: str,
) -> str:
    key = namespace_fallback_freeze_key(key_prefix, namespace)
    payload = {
        "trace_id": trace_id,
        "set_at": int(__import__("time").time()),
        "reason": reason[:2000],
        "namespace": namespace,
        "fallback": True,
    }
    await r.setex(key, ttl_sec, json.dumps(payload, ensure_ascii=False))
    logger.warning("proactive namespace freeze (fallback) key=%s trace=%s", key, trace_id)
    return key


async def try_acquire_resource_lease(
    r: Any,
    lease_key: str,
    *,
    token: str,
    ttl_sec: int,
) -> bool:
    ok = await r.set(lease_key, token, nx=True, ex=ttl_sec)
    return bool(ok)


def proactive_lease_key(prefix: str, tool: str, ref: tuple[str, str, str]) -> str:
    ns, kind, name = ref
    h = hashlib.sha256(f"{tool}|{ns}|{kind}|{name}".encode()).hexdigest()[:24]
    return f"{prefix}:lease:{h}"


DEFAULT_LEASE_PREFIX = "omni:proactive"
