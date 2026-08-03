"""Load Kubernetes client config (in-cluster first, kubeconfig fallback).

Moved from workers/k8s_tools.py (WS0 prep — running import-linter for the first
time surfaced this as a pkg/ -> workers/ violation not caught by WS1's original
7-site list). Pure, no other dependency on k8s_tools.py's own state, so it can
live here where pkg/reasoning/preflight_deployment_secret_refs.py needs it too.
workers/k8s_tools.py re-imports it under its old private name unchanged.
"""

from __future__ import annotations

from kubernetes_asyncio import config


async def load_k8s_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()
