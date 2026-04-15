"""After SDK probes pass: verify Deployment-level rollout state (not pod names)."""

from __future__ import annotations

import logging
import re
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from workers.k8s_tools import _load_k8s_config
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)

_POD_LIKE = re.compile(
    r"\b[\w.-]+-[0-9a-f]{7,12}-[a-z0-9]{5,8}\b",
    re.IGNORECASE,
)


def sanitize_probe_text_for_llm(text: str, workload_name: str) -> str:
    """Replace ephemeral pod instance names so local LLM reasons on Deployment, not pods."""
    t = text or ""
    t = _POD_LIKE.sub(f"[{workload_name}:pod]", t)
    return t[:6000]


def resolve_namespace_deployment_for_state_gate(
    mutate_args: dict[str, Any],
    tool_name: str,
    ev: AnomalyEvent,
) -> tuple[str, str]:
    """Prefer mutate args (executor truth); else labels on AnomalyEvent (deployment/workload)."""
    tn = (tool_name or "").strip().lower()
    if tn == "k8s_rollout_restart":
        ns = str(mutate_args.get("namespace") or "").strip()
        dep = str(mutate_args.get("deployment") or "").strip()
        if ns and dep:
            return ns, dep
    ns = (ev.namespace or "").strip()
    dep = (ev.deployment or "").strip()
    if ns and dep:
        return ns, dep
    from workers.diagnostic_resource import deployment_workload_from_event

    return deployment_workload_from_event(ev)


async def check_deployment_rollout_healthy(namespace: str, deployment: str) -> tuple[bool, str]:
    """
    True when Deployment has enough ready replicas vs desired (workload-level, no pod naming).
    """
    ns = (namespace or "").strip()
    dep = (deployment or "").strip()
    if not ns or not dep:
        return False, "missing_namespace_or_deployment"
    await _load_k8s_config()
    apps = client.AppsV1Api()
    try:
        try:
            d = await apps.read_namespaced_deployment(dep, ns)
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return False, "deployment_not_found"
            return False, f"api_error:{e!s}"[:500]
        spec = d.spec
        st = d.status
        desired = int(spec.replicas or 0) if spec else 0
        ready = int(st.ready_replicas or 0) if st else 0
        unavail = int(st.unavailable_replicas or 0) if st and st.unavailable_replicas else 0
        if desired == 0:
            return True, "desired_replicas=0"
        if ready < desired:
            return False, f"ready_replicas={ready} desired={desired} unavailable_replicas={unavail}"
        if unavail > 0:
            return False, f"unavailable_replicas={unavail} desired={desired}"
        return True, f"ready_replicas={ready} desired={desired}"
    finally:
        await apps.api_client.close()
