"""Durable command delivery — reference/legacy implementation (ADR-002: KHÔNG canonical).

Canonical runtime là ``gateway/routes/agent_runtime.py`` (có fencing/atomic claim/heartbeat mà
bản này không có). Module này chỉ còn phục vụ tests/demo; không thêm feature mới. Sunset: xoá
cùng Phase-3 durable Control Plane (xem ADR-002). GET is PEEK, không phải POP (fix P0).

Vì sao tồn tại: kênh cũ (`gateway/routes/agent_commands.py`) dùng ``RPOP`` khi agent
GET → command BIẾN MẤT ngay khi fetch, trước bất kỳ acknowledgement terminal nào. Agent
crash sau fetch = mất lệnh. Đây là P0: **GET ≠ ACK**. Kênh này thay bằng máy trạng thái
GIAO/RUNTIME durable:

    QUEUED → DELIVERED → ACCEPTED → RUNNING → RECONCILING
           → COMPLETED | FAILED | ESCALATED | EXPIRED

Đây là **delivery/runtime state**, KHÔNG phải noun ontology AOIP mới — sổ vận hành như
idempotency/lease/trace. Command chỉ rời vòng redelivery khi có **terminal outcome durable**
(record terminal, ZREM khỏi ready-set). Trước đó, poll luôn có thể giao lại (at-least-once).

Effectively-once: redelivery an toàn vì (1) agent giữ local inbox + idempotency ledger →
cùng command_id đã terminal thì re-report, KHÔNG re-mutate; (2) Gateway record terminal →
poll bỏ qua. Redis AOF giữ record + ready-set sống qua Gateway/Redis restart.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

# State vocabulary import từ nguồn canonical (ADR-002) — re-export giữ backward-compat
# cho mọi caller cũ đang `from aoip.agent.delivery import ST_*`.
from aoip.protocol import (  # noqa: F401 — re-export có chủ đích
    PROGRESS_ORDER as _PROGRESS_ORDER,
    ST_ACCEPTED,
    ST_COMPLETED,
    ST_DELIVERED,
    ST_ESCALATED,
    ST_EXPIRED,
    ST_FAILED,
    ST_QUEUED,
    ST_RECONCILING,
    ST_RUNNING,
    TERMINAL_STATES,
)

_DEFAULT_VISIBILITY_S = 60      # delivered-but-not-terminal → visible lại sau 60s
_TTL_TERMINAL_S = 604800        # record terminal giữ 7 ngày để dedup giao trùng muộn
_TTL_ACTIVE_S = 86400           # record đang chạy giữ 1 ngày (đủ 1 mission)


@dataclass(frozen=True)
class CommandRecord:
    """Danh tính bất biến + correlation + delivery state của MỘT command.

    Immutable identity/correlation KHÔNG bao giờ đổi sau enqueue; chỉ delivery-state
    fields (state, delivery_count, last_delivered_at, terminal_at, outcome) tiến hoá.
    """

    command_id: str
    tenant_id: str
    agent_id: str
    mission_id: str
    incident_id: str
    decision_id: str
    action_id: str
    canonical_scope: str
    payload_hash: str
    payload: dict
    created_at: float
    expires_at: float
    state: str = ST_QUEUED
    delivery_count: int = 0
    last_delivered_at: float = 0.0
    terminal_at: float = 0.0
    outcome: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "CommandRecord":
        return cls(**json.loads(raw))


def _rec_key(tenant: str, command_id: str) -> str:
    return f"omni:cmd:rec:{tenant}:{command_id}"


def _ready_key(tenant: str, agent_id: str) -> str:
    # ZSET member=command_id, score=next_visible_at. Ready-set durable ⇒ redelivery
    # sống qua Gateway/Redis restart. ZREM chỉ khi terminal outcome durable.
    return f"omni:cmd:ready:{tenant}:{agent_id}"


class DurableCommandChannel:
    """Redis-backed durable delivery. Inject async redis (real TCP hoặc FakeRedis).

    tenant-embedded mọi key (INV_NAMESPACE_ISOLATION): cùng command_id ở 2 tenant =
    2 record riêng, không đè nhau.
    """

    def __init__(self, redis, *, visibility_s: int = _DEFAULT_VISIBILITY_S) -> None:
        self._r = redis
        self._vis = visibility_s

    # ── enqueue (Omni → agent) ────────────────────────────────────────────────
    async def enqueue(self, rec: CommandRecord, *, now: float | None = None) -> CommandRecord:
        """Ghi command QUEUED + đưa vào ready-set (visible ngay). Idempotent theo command_id.

        Fail-closed: command đã hết hạn khi enqueue → EXPIRED ngay, KHÔNG vào ready-set
        (zero delivery). Hai incident khác nhau có command_id khác nhau → 2 record riêng
        (KHÔNG bị coi là trùng dù plan giống hệt).
        """
        now = time.time() if now is None else now
        existing = await self.get(rec.tenant_id, rec.command_id)
        if existing is not None:
            return existing  # đã tồn tại (redelivery của enqueue) → không tạo bản sao

        if now >= rec.expires_at:
            expired = _replace(rec, state=ST_EXPIRED, terminal_at=now,
                               outcome={"reason": "expired_before_delivery"})
            await self._r.set(_rec_key(rec.tenant_id, rec.command_id), expired.to_json(),
                              ex=_TTL_TERMINAL_S)
            return expired

        queued = _replace(rec, state=ST_QUEUED)
        await self._r.set(_rec_key(rec.tenant_id, rec.command_id), queued.to_json(),
                          ex=_TTL_ACTIVE_S)
        await self._r.zadd(_ready_key(rec.tenant_id, rec.agent_id), {rec.command_id: now})
        return queued

    # ── poll (agent GET — PEEK, non-destructive) ──────────────────────────────
    async def poll(self, tenant: str, agent_id: str, *, now: float | None = None,
                   limit: int = 10) -> list[CommandRecord]:
        """PEEK: trả command đến hạn giao, đánh dấu DELIVERED, KHÔNG xoá khỏi ready-set.

        Command hết hạn → EXPIRED + ZREM (zero delivery). Non-terminal command được
        bump visibility (now + visibility_s) → nếu agent không ack, sẽ visible lại =
        redelivery durable. GET KHÔNG BAO GIỜ pop record.
        """
        now = time.time() if now is None else now
        rkey = _ready_key(tenant, agent_id)
        due = await self._r.zrangebyscore(rkey, "-inf", now, start=0, num=limit)

        out: list[CommandRecord] = []
        for cid in due:
            rec = await self.get(tenant, cid)
            if rec is None:
                await self._r.zrem(rkey, cid)          # record biến mất (TTL) → dọn ready
                continue
            if rec.state in TERMINAL_STATES:
                await self._r.zrem(rkey, cid)          # đã terminal → thôi redelivery
                continue
            if now >= rec.expires_at:
                await self._expire(rec, now)
                await self._r.zrem(rkey, cid)
                continue
            delivered = _replace(rec, state=ST_DELIVERED,
                                 delivery_count=rec.delivery_count + 1, last_delivered_at=now)
            await self._save_active(delivered)
            await self._r.zadd(rkey, {cid: now + self._vis})   # bump visibility
            out.append(delivered)
        return out

    # ── acknowledgement protocol ──────────────────────────────────────────────
    async def mark_accepted(self, tenant: str, command_id: str,
                            *, now: float | None = None) -> CommandRecord | None:
        return await self._advance(tenant, command_id, ST_ACCEPTED, now=now)

    async def mark_progress(self, tenant: str, command_id: str, phase: str,
                            *, now: float | None = None) -> CommandRecord | None:
        """phase ∈ {RUNNING, RECONCILING}. Không cho lùi altitude; không rời terminal."""
        if phase not in (ST_RUNNING, ST_RECONCILING):
            raise ValueError(f"progress phase không hợp lệ: {phase!r}")
        return await self._advance(tenant, command_id, phase, now=now)

    async def record_terminal(self, tenant: str, command_id: str, *, state: str,
                              outcome: dict, now: float | None = None) -> CommandRecord | None:
        """Terminal report từ agent. Ghi outcome durable + ZREM ready-set (stop redelivery).

        **Đây là terminal acknowledgement**: khi trả về non-None với state terminal + đã
        persist, agent được phép archive local inbox. Idempotent — report lại cùng terminal
        (redelivery sau khi đã terminal) trả về record terminal cũ, KHÔNG đổi outcome, KHÔNG
        tạo mutation. Duplicate delivery ⇒ zero duplicate mutation.
        """
        now = time.time() if now is None else now
        if state not in TERMINAL_STATES:
            raise ValueError(f"state không phải terminal: {state!r}")
        rec = await self.get(tenant, command_id)
        if rec is None:
            return None
        if rec.state in TERMINAL_STATES:
            await self._r.zrem(_ready_key(tenant, rec.agent_id), command_id)
            return rec                                  # đã terminal → ack lại, giữ nguyên
        term = _replace(rec, state=state, terminal_at=now, outcome=outcome)
        await self._r.set(_rec_key(tenant, command_id), term.to_json(), ex=_TTL_TERMINAL_S)
        await self._r.zrem(_ready_key(tenant, rec.agent_id), command_id)
        return term

    # ── read models ───────────────────────────────────────────────────────────
    async def get(self, tenant: str, command_id: str) -> CommandRecord | None:
        raw = await self._r.get(_rec_key(tenant, command_id))
        return CommandRecord.from_json(raw) if raw else None

    async def inflight(self, tenant: str, agent_id: str) -> list[str]:
        return list(await self._r.zrange(_ready_key(tenant, agent_id), 0, -1))

    # ── internals ─────────────────────────────────────────────────────────────
    async def _advance(self, tenant: str, command_id: str, target: str,
                       *, now: float | None) -> CommandRecord | None:
        now = time.time() if now is None else now
        rec = await self.get(tenant, command_id)
        if rec is None or rec.state in TERMINAL_STATES:
            return None                                 # không advance record terminal/mất
        updated = _replace(rec, state=target, last_delivered_at=rec.last_delivered_at)
        await self._save_active(updated)
        # advance = agent đang xử lý → gia hạn visibility để tránh redelivery giữa chừng
        await self._r.zadd(_ready_key(tenant, rec.agent_id), {command_id: now + self._vis})
        return updated

    async def _expire(self, rec: CommandRecord, now: float) -> CommandRecord:
        expired = _replace(rec, state=ST_EXPIRED, terminal_at=now,
                           outcome={"reason": "expired_before_terminal"})
        await self._r.set(_rec_key(rec.tenant_id, rec.command_id), expired.to_json(),
                          ex=_TTL_TERMINAL_S)
        return expired

    async def _save_active(self, rec: CommandRecord) -> None:
        await self._r.set(_rec_key(rec.tenant_id, rec.command_id), rec.to_json(),
                          ex=_TTL_ACTIVE_S)


def _replace(rec: CommandRecord, **changes) -> CommandRecord:
    from dataclasses import replace
    return replace(rec, **changes)
