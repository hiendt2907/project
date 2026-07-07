"""HTTP client to Omni Gateway — register + emit evidence."""
from __future__ import annotations

import asyncio
import logging
import platform as _sys_platform
import socket
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_RETRY_DELAYS = (1.0, 2.0, 4.0)


def _detect_local_ip(gateway_url: str) -> str:
    """Best-effort local (LAN-facing) IP — used server-side to resolve
    cross-host `connects_to` facts from connection_scan remote_ip. UDP
    connect() never sends a packet; it only asks the kernel to pick the
    route/source-interface it would use to reach `host`, which is the same
    interface `ss -tnp` reports for real outbound connections to peer hosts.
    request.client.host is NOT reliable here — multiple VMs behind the same
    NAT egress (e.g. OrbStack shared gateway) all appear as one IP to the
    Omni gateway, making host resolution ambiguous."""
    host = urlparse(gateway_url).hostname or "8.8.8.8"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def _make_transport() -> Any:
    """Force IPv4 transport — nhiều VM/bare metal không có IPv6 routing."""
    import httpx
    return httpx.AsyncHTTPTransport(
        local_address="0.0.0.0",  # bind IPv4, không dùng ::1
    )


def _make_client(headers: dict, base_url: str) -> Any:
    import httpx
    return httpx.AsyncClient(
        headers=headers,
        base_url=base_url,
        transport=_make_transport(),
    )


async def _post(client: Any, url: str, payload: dict) -> dict:
    resp = await client.post(url, json=payload, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


async def _with_retry(client: Any, url: str, payload: dict) -> dict | None:
    for delay in (None, *_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await _post(client, url, payload)
        except Exception as exc:
            logger.warning("[emitter] POST %s failed: %s", url, exc)
    logger.error("[emitter] POST %s failed after retries — dropping batch", url)
    return None


class OmniEmitter:
    def __init__(
        self, gateway_url: str, api_key: str, agent_id: str, hostname: str, tenant_id: str = "default"
    ) -> None:
        self._base = gateway_url
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Omni-Agent-Id": agent_id,
        }
        self._agent_id = agent_id
        self._hostname = hostname
        self._tenant_id = tenant_id

    async def register(
        self,
        capabilities: list[str],
        version: str = "1.0.0",
        k8s_namespace: str = "",
        bundle_sha256: str = "",
    ) -> dict[str, float] | None:
        """Register/heartbeat with the gateway.

        Returns the anomaly-threshold bundle pushed by Omni (resolved from
        omni_admin runtime flags), or None on failure. Tuning thresholds
        server-side avoids redeploying the agent on customer hosts.
        """
        payload = {
            "agent_id": self._agent_id,
            "hostname": self._hostname,
            "version": version,
            "capabilities": capabilities,
            "platform": _sys_platform.system().lower(),
            "k8s_namespace": k8s_namespace,
            "tenant_id": self._tenant_id,
            "local_ip": _detect_local_ip(self._base),
            "bundle_sha256": bundle_sha256,
        }
        async with _make_client(self._headers, self._base) as client:
            result = await _with_retry(client, "/webhook/agent/register", payload)
            if result:
                logger.info("[emitter] registered agent_id=%s ttl=%s", self._agent_id, result.get("ttl"))
                config = result.get("config") or {}
                thresholds = config.get("thresholds") if isinstance(config, dict) else None
                return thresholds if isinstance(thresholds, dict) else None
            return None

    async def emit(self, evidence_list: list[dict]) -> int:
        """POST evidence batch. Returns number of items enqueued by gateway."""
        if not evidence_list:
            return 0
        payload = {
            "agent_id": self._agent_id,
            "hostname": self._hostname,
            "tenant_id": self._tenant_id,
            "evidence": evidence_list,
        }
        async with _make_client(self._headers, self._base) as client:
            result = await _with_retry(client, "/webhook/agent/evidence", payload)
            enqueued = result.get("enqueued", 0) if result else 0
            logger.info("[emitter] emitted evidence enqueued=%d", enqueued)
            return enqueued

    async def upload_profile(self, profile: dict) -> bool:
        """POST VM discovery profile to gateway for analyst use."""
        async with _make_client(self._headers, self._base) as client:
            result = await _with_retry(client, "/webhook/agent/profile", profile)
            ok = result is not None and result.get("status") == "stored"
            logger.info("[emitter] profile uploaded ok=%s", ok)
            return ok

    async def poll_commands(self) -> list[dict]:
        """GET pending diagnostic commands from Omni gateway."""
        url = f"/webhook/agent/commands/{self._agent_id}"
        try:
            import httpx
            async with httpx.AsyncClient(
                headers=self._headers,
                base_url=self._base,
                transport=_make_transport(),
            ) as client:
                resp = await client.get(url, timeout=10.0)
                if resp.status_code == 200:
                    return resp.json().get("commands", [])
        except Exception as exc:
            logger.warning("[emitter] poll_commands failed: %s", exc)
        return []

    async def submit_command_results(self, results: list[dict]) -> bool:
        """POST command execution results back to gateway."""
        if not results:
            return True
        payload = {
            "agent_id": self._agent_id,
            "results": results,
        }
        async with _make_client(self._headers, self._base) as client:
            result = await _with_retry(client, "/webhook/agent/command-result", payload)
            ok = result is not None
            logger.info("[emitter] submitted %d command results ok=%s", len(results), ok)
            return ok
