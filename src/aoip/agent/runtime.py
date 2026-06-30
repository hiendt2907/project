"""RemoteAgent runtime — vòng đời worker (EPIC 2).

Install→Identity→Register→Heartbeat→Mission Pull→Observe→Report. Agent là SENSOR/
worker mỏng: nó cầm transport (cách chạm host), một identity ổn định, và nói
chuyện với Omni (control plane). Discovery/Mission nặng vẫn tái dùng runtime đã có.

KHÔNG noun mới: AgentStatus là Derived KPI (dashboard khách hàng thấy), không
persist. Mission/Capability đều dùng object đã có.
"""
from __future__ import annotations

from dataclasses import dataclass

from aoip.agent.identity import AgentIdentity, derive_identity


@dataclass(frozen=True)
class AgentStatus:
    """KPI khách hàng thấy — không phải elegance, mà là tiến độ tiếp nhận hệ thống."""

    agent_id: str
    host: str
    tenant: str
    installed: bool
    registered: bool
    heartbeats: int
    knowledge_coverage: float = 0.0
    questions_outstanding: int = 0
    capability_k: float = 0.0
    next_mission: str | None = None

    def render(self) -> str:
        check = lambda b: "✔" if b else "✗"  # noqa: E731
        return (
            f"Agent {self.agent_id} @ {self.host} (tenant={self.tenant})\n"
            f"  Installed       {check(self.installed)}\n"
            f"  Registered      {check(self.registered)}\n"
            f"  Heartbeat       {check(self.heartbeats > 0)} ({self.heartbeats})\n"
            f"  Knowledge Cov.  {self.knowledge_coverage:.0%}\n"
            f"  Questions       {self.questions_outstanding}\n"
            f"  Capability(K)   {self.capability_k:.2f}\n"
            f"  Next Mission    {self.next_mission or '—'}"
        )


class RemoteAgent:
    """Worker mỏng: cầm transport (chạm host) + OmniClient (nói chuyện Control Plane).

    KHÔNG biết Control Plane là in-process hay HTTP — chỉ thấy interface OmniClient.
    Heartbeat đếm cục bộ (không reach vào backend) → abstraction sạch.
    """

    def __init__(self, *, transport, tenant: str, omni,
                 version: str = "1.0.0", capabilities: list[str] | None = None) -> None:
        self.transport = transport
        self.omni = omni  # OmniClient
        self.identity: AgentIdentity = derive_identity(transport, tenant)
        self._version = version
        self._capabilities = capabilities or ["discovery", "understand_host"]
        self._registered = False
        self._heartbeats = 0
        self._active_cmd_id: str | None = None

    async def register(self) -> None:
        await self.omni.register(
            self.identity, version=self._version,
            capabilities=self._capabilities, platform="linux",
        )
        self._registered = True

    async def heartbeat(self) -> None:
        await self.omni.heartbeat(self.identity)
        self._heartbeats += 1

    async def pull_mission(self) -> str | None:
        missions = await self.omni.fetch_missions(self.identity.agent_id)
        if not missions:
            return None
        self._active_cmd_id = missions[0]["cmd_id"]
        return missions[0]["goal"]

    async def report_result(self, *, rc: int = 0, stdout: str = "") -> None:
        await self.omni.submit_result(
            self.identity.agent_id, cmd_id=self._active_cmd_id or "", rc=rc, stdout=stdout
        )

    async def report_evidence(self, items: list[dict]) -> None:
        await self.omni.submit_evidence(
            self.identity.agent_id, hostname=self.identity.host,
            tenant=self.identity.tenant, items=items,
        )

    def status(
        self,
        *,
        knowledge_coverage: float = 0.0,
        questions_outstanding: int = 0,
        capability_k: float = 0.0,
        next_mission: str | None = None,
    ) -> AgentStatus:
        return AgentStatus(
            agent_id=self.identity.agent_id,
            host=self.identity.host,
            tenant=self.identity.tenant,
            installed=True,
            registered=self._registered,
            heartbeats=self._heartbeats,
            knowledge_coverage=knowledge_coverage,
            questions_outstanding=questions_outstanding,
            capability_k=capability_k,
            next_mission=next_mission,
        )
