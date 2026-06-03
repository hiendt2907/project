"""K8s read-only diagnostics — list pods/events if kubeconfig is available."""
from __future__ import annotations

import logging
from typing import Any

from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)


async def collect_k8s_status(namespace: str, hostname: str) -> list[dict[str, Any]]:
    """Return pod status evidence envelopes. Returns [] if K8s is not reachable."""
    try:
        from kubernetes_asyncio import client, config  # type: ignore
        from kubernetes_asyncio.client.rest import ApiException  # type: ignore
    except ImportError:
        logger.debug("[collector.k8s] kubernetes-asyncio not installed — skipping")
        return []

    try:
        try:
            await config.load_kube_config()
        except Exception:
            config.load_incluster_config()
    except Exception as exc:
        logger.debug("[collector.k8s] no kubeconfig: %s", exc)
        return []

    results: list[dict[str, Any]] = []

    async with client.ApiClient() as api:
        v1 = client.CoreV1Api(api)
        try:
            kwargs: dict[str, Any] = {}
            if namespace:
                pods = await v1.list_namespaced_pod(namespace, **kwargs)
            else:
                pods = await v1.list_pod_for_all_namespaces(**kwargs)
        except ApiException as exc:
            logger.warning("[collector.k8s] list pods failed: %s", exc)
            return []

        not_ready: list[dict[str, Any]] = []
        total = 0
        for pod in pods.items:
            total += 1
            phase = pod.status.phase if pod.status else "Unknown"
            ready = all(
                c.ready for c in (pod.status.conditions or []) if c.type == "Ready"
            )
            if phase not in ("Running", "Succeeded") or not ready:
                not_ready.append({
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "phase": phase,
                    "ready": ready,
                })

        result = "FAILED" if not_ready else "PASSED"
        hint = (
            f"[{hostname}] K8s: {len(not_ready)}/{total} pods not ready"
            if not_ready
            else f"[{hostname}] K8s: all {total} pods healthy"
        )

        env = build_envelope(
            probe="remote_k8s_pod_status",
            lane="SYS_HARD_FAIL",
            result=result,
            extracted_fact={
                "total_pods": total,
                "not_ready_count": len(not_ready),
                "not_ready_pods": not_ready[:10],
                "namespace_filter": namespace or "all",
            },
            alert_rule="RemoteK8sPodsNotReady" if not_ready else "RemoteK8sHealthy",
            alert_hint=hint,
            symptom_group="pod_container_state",
            namespace=namespace or hostname,
        )
        results.append(env)

    return results
