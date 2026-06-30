"""Omni Control Plane — não của hệ thống (EPIC 2, khởi đầu).

Reviewer: "Remote Agent chỉ là worker. Não nằm ở Omni." Omni KHÔNG phải dashboard;
nó là Mission Scheduler + Capability Manager + Registry + Communication Hub. Bản
khởi đầu này giữ in-process (registry + mission queue per agent) để đóng vòng
Register→Heartbeat→Assign→Pull thật; backend bền vững (PG/Redis/Kafka đã có trong
codebase Omni) sẽ wire khi deployment thật yêu cầu — runtime ép, không suy diễn.
"""
from __future__ import annotations

from collections import deque


class Omni:
    def __init__(self) -> None:
        self._agents: dict[str, dict] = {}
        self._missions: dict[str, deque[str]] = {}

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
