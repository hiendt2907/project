"""E2E environment resolver (Python mirror of scripts/lib/e2e_env.sh).

Reality (2026-06-10): gateway via Traefik ingress http://gateway.ai-agent.local
with HTTP Bearer auth; Kafka/Redis resolved to ClusterIP via kubectl (OrbStack
routes ClusterIPs from the host). E2E_* / OMNI_* env vars override everything.
"""
from __future__ import annotations

import base64
import os
import subprocess

NS = os.getenv("NS", "multi-agent")


def _kubectl(*args: str) -> str:
    try:
        out = subprocess.run(
            ["kubectl", *args], capture_output=True, text=True, timeout=15, check=False
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _cluster_ip(svc: str, port: int) -> str:
    ip = _kubectl("get", "svc", "-n", NS, svc, "-o", "jsonpath={.spec.clusterIP}")
    if ip and ip != "None":
        return f"{ip}:{port}"
    return ""


def gateway_url() -> str:
    return os.getenv("OMNI_GATEWAY_URL") or os.getenv("E2E_GATEWAY_URL") or "http://gateway.ai-agent.local"


def gateway_api_key() -> str:
    for env in ("OMNI_GATEWAY_API_KEY", "E2E_GATEWAY_API_KEY"):
        v = os.getenv(env)
        if v:
            return v
    raw = _kubectl(
        "get", "secret", "-n", NS, "omni-gateway-secret",
        "-o", "jsonpath={.data.OMNI_GATEWAY_API_KEY}",
    )
    try:
        return base64.b64decode(raw).decode() if raw else ""
    except Exception:
        return ""


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {gateway_api_key()}"}


def kafka_bootstrap() -> str:
    return (
        os.getenv("E2E_KAFKA_BOOTSTRAP")
        or os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS")
        or _cluster_ip("kafka", 9092)
    )


def redis_url() -> str:
    v = os.getenv("E2E_REDIS_MA_URL") or os.getenv("OMNI_REDIS_URL")
    if v:
        return v
    ip = _cluster_ip("redis", 6379)
    return f"redis://{ip}/0" if ip else ""
