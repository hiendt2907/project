"""Trace Spine — mọi transition runtime phát ra một event TƯƠNG QUAN, đọc lại được.

Vì sao tồn tại (DoD mới): một thay đổi backend CHƯA hoàn thành nếu hành vi của nó chỉ
hiểu được qua source/Redis/log/test. Track A (Runtime Safety) và Track B (Operator
Visibility) đi cùng nhau: mỗi safety gate / mutation phase / reconcile / verify ở Track A
PHẢI quan sát được ở Track B ngay lập tức.

KHÔNG source-of-truth thứ hai: đây là READ-MODEL mỏng dựng từ chính runtime — event ghi
vào Redis (durable, tenant-isolated), console chỉ đọc lại. Correlation object cũng chính
là danh tính bất biến dùng cho idempotency key (#4) và audit (#5) — một spine, dùng lại.

KHÔNG noun ontology mới: RuntimeEvent/Correlation là Derived runtime value (sổ vận hành),
không phải entity tri thức.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

# ── 12 event types (mọi transition quan trọng) ───────────────────────────────
EV_COMMAND_RECEIVED = "COMMAND_RECEIVED"
EV_IDEMPOTENCY_CLAIMED = "IDEMPOTENCY_CLAIMED"
EV_LEASE_ACQUIRED = "LEASE_ACQUIRED"
EV_APPROVAL_VALIDATED = "APPROVAL_VALIDATED"
EV_APPROVAL_REJECTED = "APPROVAL_REJECTED"
EV_MUTATION_STARTED = "MUTATION_STARTED"
EV_VERIFYING = "VERIFYING"
EV_RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
EV_RECONCILED = "RECONCILED"
EV_COMPLETED = "COMPLETED"
EV_ESCALATED = "ESCALATED"
EV_ABORTED = "ABORTED"

TERMINAL_EVENTS = frozenset({EV_COMPLETED, EV_ESCALATED, EV_ABORTED, EV_RECONCILED})


def canonical_scope(tenant: str, node: str) -> str:
    """Scope chuẩn hoá + NHÚNG tenant (#5). Cùng target name, khác tenant → khác scope."""
    norm = re.sub(r"\s+", "", node).strip().lower()
    return f"{tenant}:{norm}"


@dataclass(frozen=True)
class Correlation:
    """Danh tính bất biến của một mutation intent — spine cho trace + idempotency + audit.

    correlation_id gom theo INCIDENT: một incident = một timeline E2E
    (Observation→Diagnosis→Decision→Approval→Action→Verification→Outcome), bất kể có bao
    nhiêu command/redelivery.
    """

    tenant: str
    agent_id: str
    mission_id: str
    incident_id: str
    decision_id: str
    action_id: str
    command_id: str
    canonical_scope: str

    @property
    def correlation_id(self) -> str:
        raw = f"{self.tenant}|{self.incident_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_fields(self) -> dict:
        return {
            "tenant_id": self.tenant, "agent_id": self.agent_id, "mission_id": self.mission_id,
            "incident_id": self.incident_id, "decision_id": self.decision_id,
            "action_id": self.action_id, "command_id": self.command_id,
            "canonical_scope": self.canonical_scope, "correlation_id": self.correlation_id,
        }


def _tl_key(tenant: str, correlation_id: str) -> str:
    return f"trace:tl:{tenant}:{correlation_id}"


def _seq_key(tenant: str, correlation_id: str) -> str:
    return f"trace:seq:{tenant}:{correlation_id}"


def _index_key(tenant: str) -> str:
    return f"trace:index:{tenant}"


def _pending_key(tenant: str) -> str:
    return f"trace:pending_approval:{tenant}"


class RuntimeTrace:
    """Read-model mỏng trên Redis. Inject async redis (real TCP hoặc FakeRedis)."""

    def __init__(self, redis) -> None:
        self._r = redis

    async def emit(self, event_type: str, corr: Correlation, *, state_before: str,
                   state_after: str, reason: str, evidence_refs: tuple[str, ...] = (),
                   ts: float, source_version: int | None = None) -> dict:
        """Ghi 1 event vào timeline của incident + đăng ký index tenant. Trả block đã ghi.

        Consistency: mỗi event mang ``seq`` đơn điệu (do trace cấp) + ``source_version``
        (phiên bản state của runtime safety). Operator phát hiện thiếu/đảo transition qua
        khoảng trống seq. Trace là READ MODEL — KHÔNG bao giờ là input điều khiển mutation.
        """
        seq = int(await self._r.incr(_seq_key(corr.tenant, corr.correlation_id)))
        block = {
            "seq": seq, "event_type": event_type, "timestamp": ts,
            "source_version": source_version,
            "state_before": state_before, "state_after": state_after,
            "reason": reason, "evidence_refs": list(evidence_refs),
            **corr.as_fields(),
        }
        await self._r.rpush(_tl_key(corr.tenant, corr.correlation_id), json.dumps(block))
        await self._r.sadd(_index_key(corr.tenant), corr.correlation_id)
        return block

    async def timeline(self, tenant: str, correlation_id: str) -> list[dict]:
        raw = await self._r.lrange(_tl_key(tenant, correlation_id), 0, -1)
        return [json.loads(x) for x in raw]

    async def list_timelines(self, tenant: str) -> list[str]:
        return sorted(await self._r.smembers(_index_key(tenant)))

    # ── pending approvals: index riêng cho Human Inbox ────────────────────────
    async def mark_pending_approval(self, corr: Correlation, *, reason: str, ts: float) -> None:
        entry = json.dumps({"reason": reason, "timestamp": ts, **corr.as_fields()})
        await self._r.hset(_pending_key(corr.tenant), corr.correlation_id, entry)

    async def clear_pending_approval(self, corr: Correlation) -> None:
        await self._r.hdel(_pending_key(corr.tenant), corr.correlation_id)

    async def pending_approvals(self, tenant: str) -> list[dict]:
        raw = await self._r.hgetall(_pending_key(tenant))
        return [json.loads(v) for v in raw.values()]
