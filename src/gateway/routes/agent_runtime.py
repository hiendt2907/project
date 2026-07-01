"""Durable delivery of MUTATING recovery commands — Living Operations Runtime.

Khác kênh chẩn đoán read-only ở ``agent_commands.py`` (RPOP peek-pop, fire-and-forget cho
stat/ls/…): kênh này giao **mutating recovery command** với at-least-once DURABLE + máy
trạng thái giao/runtime. **GET = PEEK, KHÔNG POP** — command chỉ rời vòng redelivery khi có
terminal outcome durable (fix P0: fetch ≠ ack).

    QUEUED → DELIVERED → ACCEPTED → RUNNING → RECONCILING
           → COMPLETED | FAILED | ESCALATED | EXPIRED

Redis layout (tenant-embedded — INV_NAMESPACE_ISOLATION):
  omni:cmd:rec:{tenant}:{command_id}     STRING JSON record (identity+correlation+state)
  omni:cmd:ready:{tenant}:{agent_id}     ZSET member=command_id score=next_visible_at

Đây là bản twin phía Gateway của ``aoip.agent.delivery.DurableCommandChannel`` — KHÔNG import
aoip (Dockerfile.gateway không COPY src/aoip). Hợp đồng HTTP (không phải key Redis) là ranh
giới với agent; hai bên phải giữ contract này đồng bộ.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.tenant_context import get_tenant_ctx, is_admin_ctx, require_agent_tenant

router = APIRouter(prefix="/webhook/agent/rt", tags=["agent-runtime-delivery"])

ST_QUEUED, ST_DELIVERED, ST_ACCEPTED = "QUEUED", "DELIVERED", "ACCEPTED"
ST_RUNNING, ST_RECONCILING = "RUNNING", "RECONCILING"
ST_COMPLETED, ST_FAILED, ST_ESCALATED, ST_EXPIRED = "COMPLETED", "FAILED", "ESCALATED", "EXPIRED"
TERMINAL = frozenset({ST_COMPLETED, ST_FAILED, ST_ESCALATED, ST_EXPIRED})
_PROGRESS = frozenset({ST_RUNNING, ST_RECONCILING})

_VISIBILITY_S = 60
_TTL_TERMINAL_S = 604800
_TTL_ACTIVE_S = 86400
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")


def _rec_key(tenant: str, command_id: str) -> str:
    return f"omni:cmd:rec:{tenant}:{command_id}"


def _ready_key(tenant: str, agent_id: str) -> str:
    return f"omni:cmd:ready:{tenant}:{agent_id}"


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _tenant_of(request: Request, body_tenant: str) -> str:
    ctx = get_tenant_ctx(request)
    return body_tenant if is_admin_ctx(ctx) else (ctx.tenant_id if ctx else body_tenant)


# ── Models ───────────────────────────────────────────────────────────────────

class EnqueueRuntimeCommand(BaseModel):
    command_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="", max_length=128)
    mission_id: str = Field(default="", max_length=128)
    incident_id: str = Field(default="", max_length=128)
    decision_id: str = Field(default="", max_length=128)
    action_id: str = Field(default="", max_length=128)
    canonical_scope: str = Field(default="", max_length=256)
    payload_hash: str = Field(default="", max_length=128)
    payload: dict = Field(default_factory=dict)
    ttl_s: int = Field(default=300, ge=1, le=3600)


class AckDelivered(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="", max_length=128)
    command_id: str = Field(min_length=1, max_length=128)


class AckProgress(AckDelivered):
    phase: str = Field(min_length=1, max_length=32)


class Terminal(AckDelivered):
    state: str = Field(min_length=1, max_length=32)
    outcome: dict = Field(default_factory=dict)


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/commands/enqueue")
async def enqueue_command(body: EnqueueRuntimeCommand, request: Request) -> JSONResponse:
    """Omni enqueues một mutating recovery command (durable QUEUED). Idempotent theo command_id.

    Fail-closed: đã hết hạn khi enqueue → EXPIRED ngay, KHÔNG vào ready-set (zero delivery).
    Hai incident khác nhau ⇒ command_id khác nhau ⇒ 2 record riêng (không dedup nhầm).
    """
    redis = _get_redis(request)
    if not _AGENT_ID_RE.fullmatch(body.agent_id):
        raise HTTPException(status_code=422, detail="Invalid agent_id")
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    tenant = _tenant_of(request, body.tenant_id)
    now = int(time.time())
    expires_at = now + body.ttl_s

    rkey = _rec_key(tenant, body.command_id)
    if await redis.get(rkey) is not None:
        existing = json.loads(await redis.get(rkey))
        return JSONResponse(content={"command_id": body.command_id, "state": existing["state"],
                                     "duplicate": True})

    base = {
        "command_id": body.command_id, "tenant_id": tenant, "agent_id": body.agent_id,
        "mission_id": body.mission_id, "incident_id": body.incident_id,
        "decision_id": body.decision_id, "action_id": body.action_id,
        "canonical_scope": body.canonical_scope, "payload_hash": body.payload_hash,
        "payload": body.payload, "created_at": now, "expires_at": expires_at,
        "delivery_count": 0, "last_delivered_at": 0, "terminal_at": 0, "outcome": {},
    }
    if now >= expires_at:
        base.update(state=ST_EXPIRED, terminal_at=now, outcome={"reason": "expired_before_delivery"})
        await redis.set(rkey, json.dumps(base), ex=_TTL_TERMINAL_S)
        return JSONResponse(content={"command_id": body.command_id, "state": ST_EXPIRED})

    base["state"] = ST_QUEUED
    await redis.set(rkey, json.dumps(base), ex=_TTL_ACTIVE_S)
    await redis.zadd(_ready_key(tenant, body.agent_id), {body.command_id: now})
    return JSONResponse(content={"command_id": body.command_id, "state": ST_QUEUED})


@router.get("/commands/{agent_id}")
async def poll_commands(agent_id: str, request: Request) -> JSONResponse:
    """PEEK durable: trả command đến hạn, đánh dấu DELIVERED, KHÔNG xoá. GET không bao giờ pop."""
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise HTTPException(status_code=422, detail="Invalid agent_id")
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)
    await require_agent_tenant(redis, agent_id, ctx)
    tenant = ctx.tenant_id if ctx and not is_admin_ctx(ctx) else None
    # admin poll cần tenant tường minh; agent key = tenant của chính agent
    if tenant is None:
        # dò tenant từ registry của agent
        reg = await redis.get(f"omni:remote_agent:registry:{agent_id}")
        tenant = json.loads(reg).get("tenant_id") if reg else ""
    now = int(time.time())
    rkey = _ready_key(tenant, agent_id)
    due = await redis.zrangebyscore(rkey, "-inf", now, start=0, num=10)

    out: list[dict] = []
    for cid in due:
        raw = await redis.get(_rec_key(tenant, cid))
        if raw is None:
            await redis.zrem(rkey, cid)
            continue
        rec = json.loads(raw)
        if rec["state"] in TERMINAL:
            await redis.zrem(rkey, cid)
            continue
        if now >= rec["expires_at"]:
            rec.update(state=ST_EXPIRED, terminal_at=now, outcome={"reason": "expired_before_terminal"})
            await redis.set(_rec_key(tenant, cid), json.dumps(rec), ex=_TTL_TERMINAL_S)
            await redis.zrem(rkey, cid)
            continue
        rec.update(state=ST_DELIVERED, delivery_count=rec["delivery_count"] + 1,
                   last_delivered_at=now)
        await redis.set(_rec_key(tenant, cid), json.dumps(rec), ex=_TTL_ACTIVE_S)
        await redis.zadd(rkey, {cid: now + _VISIBILITY_S})
        out.append(rec)
    return JSONResponse(content={"commands": out})


async def _advance(redis, tenant: str, command_id: str, agent_id: str, target: str) -> dict | None:
    raw = await redis.get(_rec_key(tenant, command_id))
    if raw is None:
        return None
    rec = json.loads(raw)
    if rec["state"] in TERMINAL:
        return rec
    now = int(time.time())
    rec["state"] = target
    await redis.set(_rec_key(tenant, command_id), json.dumps(rec), ex=_TTL_ACTIVE_S)
    await redis.zadd(_ready_key(tenant, agent_id), {command_id: now + _VISIBILITY_S})
    return rec


@router.post("/commands/accept")
async def accept_command(body: AckDelivered, request: Request) -> JSONResponse:
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    tenant = _tenant_of(request, body.tenant_id)
    rec = await _advance(redis, tenant, body.command_id, body.agent_id, ST_ACCEPTED)
    if rec is None:
        raise HTTPException(status_code=404, detail="command not found")
    return JSONResponse(content={"command_id": body.command_id, "state": rec["state"]})


@router.post("/commands/progress")
async def progress_command(body: AckProgress, request: Request) -> JSONResponse:
    if body.phase not in _PROGRESS:
        raise HTTPException(status_code=422, detail=f"invalid phase {body.phase!r}")
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    tenant = _tenant_of(request, body.tenant_id)
    rec = await _advance(redis, tenant, body.command_id, body.agent_id, body.phase)
    if rec is None:
        raise HTTPException(status_code=404, detail="command not found")
    return JSONResponse(content={"command_id": body.command_id, "state": rec["state"]})


@router.post("/commands/terminal")
async def terminal_command(body: Terminal, request: Request) -> JSONResponse:
    """Agent report terminal outcome. Ghi durable + ZREM ready (stop redelivery) + trả ACK.

    ``acknowledged: true`` LÀ terminal acknowledgement — agent nhận rồi mới archive local inbox.
    Idempotent: report lại cùng command đã terminal → trả record cũ, KHÔNG đổi outcome, zero
    mutation. Duplicate delivery ⇒ zero duplicate mutation.
    """
    if body.state not in TERMINAL:
        raise HTTPException(status_code=422, detail=f"state không terminal: {body.state!r}")
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    tenant = _tenant_of(request, body.tenant_id)
    raw = await redis.get(_rec_key(tenant, body.command_id))
    if raw is None:
        raise HTTPException(status_code=404, detail="command not found")
    rec = json.loads(raw)
    now = int(time.time())
    if rec["state"] in TERMINAL:
        await redis.zrem(_ready_key(tenant, body.agent_id), body.command_id)
        return JSONResponse(content={"acknowledged": True, "command_id": body.command_id,
                                     "state": rec["state"], "idempotent": True})
    rec.update(state=body.state, terminal_at=now, outcome=body.outcome)
    await redis.set(_rec_key(tenant, body.command_id), json.dumps(rec), ex=_TTL_TERMINAL_S)
    await redis.zrem(_ready_key(tenant, body.agent_id), body.command_id)
    return JSONResponse(content={"acknowledged": True, "command_id": body.command_id,
                                 "state": body.state, "idempotent": False})


@router.get("/commands/record/{tenant}/{command_id}")
async def get_record(tenant: str, command_id: str, request: Request) -> JSONResponse:
    """Read-model cho portal projection: trạng thái giao đầy đủ của 1 command."""
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)
    if not is_admin_ctx(ctx) and (ctx is None or ctx.tenant_id != tenant):
        raise HTTPException(status_code=403, detail="tenant mismatch")
    raw = await redis.get(_rec_key(tenant, command_id))
    if raw is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(content=json.loads(raw))
