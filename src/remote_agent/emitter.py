"""HTTP client to Omni Gateway — register + emit evidence."""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

_RETRY_DELAYS = (1.0, 2.0, 4.0)


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
    def __init__(self, gateway_url: str, api_key: str, agent_id: str, hostname: str) -> None:
        self._base = gateway_url
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Omni-Agent-Id": agent_id,
        }
        self._agent_id = agent_id
        self._hostname = hostname

    async def register(self, capabilities: list[str], version: str = "1.0.0", k8s_namespace: str = "") -> bool:
        payload = {
            "agent_id": self._agent_id,
            "hostname": self._hostname,
            "version": version,
            "capabilities": capabilities,
            "platform": "linux",
            "k8s_namespace": k8s_namespace,
        }
        async with _make_client(self._headers, self._base) as client:
            result = await _with_retry(client, "/webhook/agent/register", payload)
            if result:
                logger.info("[emitter] registered agent_id=%s ttl=%s", self._agent_id, result.get("ttl"))
                return True
            return False

    async def emit(self, evidence_list: list[dict]) -> int:
        """POST evidence batch. Returns number of items enqueued by gateway."""
        if not evidence_list:
            return 0
        payload = {
            "agent_id": self._agent_id,
            "hostname": self._hostname,
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
