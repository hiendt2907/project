"""Agent Identity — danh tính ổn định của một Remote Agent (EPIC 2 bootstrap).

Mỗi agent cần một identity bền vững (không đổi giữa các lần restart) để Omni theo
dõi capability/trust/authority per-agent. Derive deterministic từ host + tenant —
KHÔNG random mỗi lần chạy. Đây là bước 'Identity' trong Install→Identity→Register.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    host: str
    tenant: str


def derive_identity(transport, tenant: str) -> AgentIdentity:
    """Identity ổn định = hash(tenant + host). transport.target là host vật lý."""
    host = getattr(transport, "target", "localhost")
    digest = hashlib.sha256(f"{tenant}/{host}".encode()).hexdigest()[:16]
    return AgentIdentity(agent_id=f"agent-{digest}", host=host, tenant=tenant)
