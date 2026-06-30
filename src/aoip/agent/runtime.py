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
    def __init__(self, *, transport, tenant: str, omni) -> None:
        self.transport = transport
        self.omni = omni
        self.identity: AgentIdentity = derive_identity(transport, tenant)
        self._registered = False

    async def register(self) -> None:
        self.omni.register_agent(
            self.identity.agent_id, host=self.identity.host, tenant=self.identity.tenant
        )
        self._registered = True

    async def heartbeat(self) -> None:
        self.omni.receive_heartbeat(self.identity.agent_id)

    async def pull_mission(self) -> str | None:
        return self.omni.next_mission(self.identity.agent_id)

    def status(
        self,
        *,
        knowledge_coverage: float = 0.0,
        questions_outstanding: int = 0,
        capability_k: float = 0.0,
        next_mission: str | None = None,
    ) -> AgentStatus:
        rec = self.omni.agent_record(self.identity.agent_id) if self._registered else {}
        return AgentStatus(
            agent_id=self.identity.agent_id,
            host=self.identity.host,
            tenant=self.identity.tenant,
            installed=True,
            registered=self._registered,
            heartbeats=rec.get("heartbeats", 0),
            knowledge_coverage=knowledge_coverage,
            questions_outstanding=questions_outstanding,
            capability_k=capability_k,
            next_mission=next_mission,
        )
