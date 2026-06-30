"""Omni stub — SIM/TEST backend, KHÔNG phải Control Plane production.

ĐỌC KỸ (ownership): Control Plane THẬT của Omni đã tồn tại ở
``src/gateway/routes/agent_webhook.py`` + ``agent_commands.py`` (register, commands
queue, evidence — backed Redis/Kafka). File này KHÔNG phải source-of-truth; nó chỉ
là backend in-process để ``InProcessOmniClient`` chạy bootstrap/test offline mà
không cần gateway. Production: agent dùng ``HTTPOmniClient`` trỏ gateway thật.

Giữ tối thiểu: registry + mission queue + result/evidence sink để đóng vòng test.
"""
from __future__ import annotations

from collections import deque


class Omni:
    def __init__(self) -> None:
        self._agents: dict[str, dict] = {}
        self._missions: dict[str, deque[str]] = {}
        self.results: list[dict] = []
        self.evidence: list[dict] = []

    # ── Registry ─────────────────────────────────────────────────────────────
    def register_agent(self, agent_id: str, *, host: str, tenant: str) -> None:
        self._agents.setdefault(agent_id, {
            "host": host, "tenant": tenant, "state": "registered", "heartbeats": 0,
        })
        self._missions.setdefault(agent_id, deque())

    def is_registered(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def agent_record(self, agent_id: str) -> dict:
        return dict(self._agents[agent_id])

    def receive_heartbeat(self, agent_id: str) -> None:
        rec = self._agents[agent_id]
        rec["heartbeats"] += 1
        rec["state"] = "online"

    # ── Mission queue ────────────────────────────────────────────────────────
    def assign_mission(self, agent_id: str, *, goal: str) -> None:
        self._missions.setdefault(agent_id, deque()).append(goal)

    def next_mission(self, agent_id: str) -> str | None:
        queue = self._missions.get(agent_id)
        return queue.popleft() if queue else None

    # ── Result / evidence sink (đối chiếu trong test) ────────────────────────
    def record_result(self, agent_id: str, *, cmd_id: str, rc: int, stdout: str) -> None:
        self.results.append({"agent_id": agent_id, "cmd_id": cmd_id, "rc": rc, "stdout": stdout})

    def record_evidence(self, agent_id: str, *, items: list[dict]) -> None:
        self.evidence.append({"agent_id": agent_id, "items": items})
