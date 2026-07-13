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

    # ── Durable mutating-command delivery (Living Ops Runtime, /rt) ────────────
    async def poll_runtime(self, agent_id: str) -> list[dict]:
        """PEEK durable command channel (mutating). GET không pop — redelivery đến khi ack."""
        resp = await self._client.get(f"/webhook/agent/rt/commands/{agent_id}")
        resp.raise_for_status()
        return resp.json().get("commands", [])

    async def accept(self, agent_id: str, tenant_id: str, command_id: str, *,
                     delivery_attempt: int, fencing_token: str) -> None:
        await self._post_rt("/commands/accept",
                            {"agent_id": agent_id, "tenant_id": tenant_id, "command_id": command_id,
                             "delivery_attempt": delivery_attempt, "fencing_token": fencing_token})

    async def progress(self, agent_id: str, tenant_id: str, command_id: str, phase: str, *,
                       delivery_attempt: int, fencing_token: str) -> None:
        await self._post_rt("/commands/progress",
                            {"agent_id": agent_id, "tenant_id": tenant_id,
                             "command_id": command_id, "phase": phase,
                             "delivery_attempt": delivery_attempt, "fencing_token": fencing_token})

    async def report_terminal(self, agent_id: str, tenant_id: str, command_id: str,
                              state: str, outcome: dict, *,
                              delivery_attempt: int, fencing_token: str) -> dict:
        """Report terminal outcome; trả Gateway terminal acknowledgement ({acknowledged: bool}).

        ``delivery_attempt``/``fencing_token`` PHẢI khớp delivery attempt hiện tại của record ở
        Gateway — sai (stale sau redelivery, vd đã bị redeliver trong lúc mutation chạy lâu) →
        Gateway trả 409. KHÔNG raise ở đây: trả ``{"acknowledged": False, "conflict": True,
        "error": ...}`` để caller (``DeliveryLoop``) GIỮ outcome cục bộ + log, KHÔNG tự kết luận
        mutation thất bại và KHÔNG archive local inbox (retry re-report ở lần resume sau).
        """
        resp = await self._client.post(
            f"{self._BASE}/rt/commands/terminal",
            json={"agent_id": agent_id, "tenant_id": tenant_id, "command_id": command_id,
                 "state": state, "outcome": outcome, "delivery_attempt": delivery_attempt,
                 "fencing_token": fencing_token})
        if resp.status_code == 409:
            body = resp.json()
            return {"acknowledged": False, "conflict": True, "error": body.get("error", "conflict")}
        resp.raise_for_status()
        return resp.json()

    async def heartbeat_visibility(self, agent_id: str, tenant_id: str, command_id: str, *,
                                   delivery_attempt: int, fencing_token: str) -> bool:
        """Gia hạn Gateway delivery visibility trong lúc RUNNING/RECONCILING.

        Trả True nếu gia hạn thành công. Trả False (KHÔNG raise) nếu Gateway từ chối vì
        ownership/fencing (409 — stale attempt, token sai, terminal, expired, not_running):
        đây là "ownership_lost" thật, không phải lỗi mạng thoáng qua. Lỗi mạng/HTTP khác
        (5xx, timeout) VẪN raise — caller (renewal coordinator) coi đó là lỗi tạm thời,
        retry ở lần sau, KHÔNG kết luận mất ownership.
        """
        resp = await self._client.post(
            f"{self._BASE}/rt/commands/heartbeat",
            json={"agent_id": agent_id, "tenant_id": tenant_id, "command_id": command_id,
                 "delivery_attempt": delivery_attempt, "fencing_token": fencing_token})
        if resp.status_code == 409:
            return False
        resp.raise_for_status()
        return True

    async def download_release_bundle(self) -> bytes:
        """Tải release bundle từ CHÍNH gateway (IT-5). Kênh đã xác thực Bearer —
        không URL ngoài, không cần host-whitelist/SSRF guard. Caller verify
        sha256 vs release manifest trước khi cài."""
        resp = await self._client.get(f"{self._BASE}/release/bundle", timeout=120.0)
        resp.raise_for_status()
        return resp.content

    async def _post_rt(self, path: str, payload: dict) -> dict:
        resp = await self._client.post(f"/webhook/agent/rt{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
