"""OmniClient — interface AOIP nói chuyện với Omni Control Plane.

QUAN TRỌNG (ownership): AOIP KHÔNG sở hữu Control Plane. Não thật (registry,
mission queue, knowledge) nằm ở Omni — đã có sẵn ở ``src/gateway/routes/``. AOIP
chỉ sở hữu Remote Agent runtime và tích hợp Omni qua interface này. REST/HTTP là
chi tiết hiện thực, KHÔNG được rò vào runtime — mai đổi sang gRPC/NATS/Kafka chỉ
thay implementation, không sửa agent.

Hai implementation:
  - ``InProcessOmniClient``  — bootstrap/test/sim (bọc stub ``omni.control_plane.Omni``).
  - ``HTTPOmniClient``       — gọi gateway THẬT (`/webhook/agent/*`) đang chạy production.

5 thao tác control-plane: register · heartbeat · fetch_missions · submit_result ·
submit_evidence.
"""
from __future__ import annotations

from typing import Any, Protocol

from aoip.agent.identity import AgentIdentity


class OmniClient(Protocol):
    async def register(self, identity: AgentIdentity, *, version: str,
                       capabilities: list[str], platform: str) -> None: ...
    async def heartbeat(self, identity: AgentIdentity) -> None: ...
    async def fetch_missions(self, agent_id: str) -> list[dict]: ...
    async def submit_result(self, agent_id: str, *, cmd_id: str, rc: int,
                            stdout: str) -> None: ...
    async def submit_evidence(self, agent_id: str, *, hostname: str, tenant: str,
                              items: list[dict]) -> None: ...


class InProcessOmniClient:
    """Adapter bootstrap/test — bọc stub Omni in-process. KHÔNG phải Control Plane thật."""

    def __init__(self, backend) -> None:
        self.backend = backend  # omni.control_plane.Omni (sim/test only)

    async def register(self, identity, *, version="1.0.0", capabilities=None, platform="linux") -> None:
        self.backend.register_agent(identity.agent_id, host=identity.host, tenant=identity.tenant)

    async def heartbeat(self, identity) -> None:
        self.backend.receive_heartbeat(identity.agent_id)

    async def fetch_missions(self, agent_id: str) -> list[dict]:
        goal = self.backend.next_mission(agent_id)
        return [{"cmd_id": f"local-{goal}", "goal": goal}] if goal else []

    async def submit_result(self, agent_id, *, cmd_id, rc, stdout) -> None:
        self.backend.record_result(agent_id, cmd_id=cmd_id, rc=rc, stdout=stdout)

    async def submit_evidence(self, agent_id, *, hostname, tenant, items) -> None:
        self.backend.record_evidence(agent_id, items=items)


class HTTPOmniClient:
    """Gọi Omni gateway THẬT qua HTTP. Toàn bộ REST đóng gói TRONG đây.

    ``client`` cho phép tiêm ``httpx.AsyncClient`` (vd ASGITransport để E2E in-
    process). Production: tự tạo client trỏ ``base_url`` của gateway, kèm Bearer key.
    """

    _BASE = "/webhook/agent"

    def __init__(self, base_url: str = "", *, api_key: str | None = None,
                 client: Any = None) -> None:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15.0)

    async def _post(self, path: str, payload: dict) -> dict:
        resp = await self._client.post(f"{self._BASE}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def register(self, identity, *, version="1.0.0", capabilities=None, platform="linux") -> None:
        await self._post("/register", {
            "agent_id": identity.agent_id,
            "hostname": identity.host,
            "version": version,
            "capabilities": capabilities or [],
            "platform": platform,
            "tenant_id": identity.tenant,
        })

    async def heartbeat(self, identity) -> None:
        # Gateway coi register lặp lại (≤TTL) là keepalive — không endpoint riêng.
        await self.register(identity)

    async def fetch_missions(self, agent_id: str) -> list[dict]:
        resp = await self._client.get(f"{self._BASE}/commands/{agent_id}")
        resp.raise_for_status()
        out: list[dict] = []
        for cmd in resp.json().get("commands", []):
            out.append({"cmd_id": cmd.get("cmd_id", ""),
                        "goal": cmd.get("purpose") or cmd.get("command", "")})
        return out

    async def submit_result(self, agent_id, *, cmd_id, rc, stdout) -> None:
        await self._post("/command-result", {
            "agent_id": agent_id,
            "results": [{"cmd_id": cmd_id, "rc": rc, "stdout": stdout[:8192]}],
        })

    async def submit_evidence(self, agent_id, *, hostname, tenant, items) -> None:
        await self._post("/evidence", {
            "agent_id": agent_id,
            "hostname": hostname,
            "tenant_id": tenant,
            "evidence": items,
        })

    async def aclose(self) -> None:
        await self._client.aclose()
