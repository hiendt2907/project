"""Clone literal container env from a real Pod for sandbox fidelity (no secretKeyRef resolution)."""

from __future__ import annotations

import re
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

_MAX_VARS = 40
_MAX_VAL_LEN = 1024
_SENSITIVE_NAME = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|BEARER|PRIVATE_KEY|CREDENTIAL)",
    re.I,
)


async def _load_k8s() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


def _safe_env_from_container(container: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for e in container.env or []:
        if len(out) >= _MAX_VARS:
            break
        name = (e.name or "").strip()
        if not name or _SENSITIVE_NAME.search(name):
            continue
        if e.value is not None:
            v = str(e.value)[:_MAX_VAL_LEN]
            out.append({"name": name, "value": v})
        # value_from (secret/configmap) — skip by default
    return out


async def clone_pod_env_for_sandbox(
    namespace: str,
    pod_name: str,
    container_name: str | None = None,
) -> list[dict[str, str]]:
    await _load_k8s()
    v1 = client.CoreV1Api()
    try:
        pod = await v1.read_namespaced_pod(pod_name, namespace)
    except ApiException:
        await v1.api_client.close()
        return []
    try:
        containers = list(pod.spec.containers or []) + list(pod.spec.init_containers or [])
        if not containers:
            return []
        target = None
        if container_name:
            for c in containers:
                if (c.name or "") == container_name:
                    target = c
                    break
        if target is None:
            target = containers[0]
        return _safe_env_from_container(target)
    finally:
        await v1.api_client.close()


async def clone_pod_labels_for_sandbox(namespace: str, pod_name: str) -> dict[str, str]:
    await _load_k8s()
    v1 = client.CoreV1Api()
    try:
        pod = await v1.read_namespaced_pod(pod_name, namespace)
    except ApiException:
        await v1.api_client.close()
        return {}
    try:
        labels = dict(pod.metadata.labels or {})
        safe: dict[str, str] = {}
        for k, v in list(labels.items())[:24]:
            ks = str(k)[:63]
            vs = str(v)[:128]
            if ks and vs:
                safe[ks] = vs
        return safe
    finally:
        await v1.api_client.close()
