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

Đây là implementation CANONICAL của command delivery protocol (ADR-002); state vocabulary
import từ ``aoip.protocol`` (nguồn chân lý duy nhất — bảng TERMINAL trong Lua script bên dưới
được contract test giữ đồng bộ). Hợp đồng HTTP (không phải key Redis) là ranh giới với agent.

## Atomic claim + fencing (delivery ownership)

Trước: ``poll_commands`` GET record rồi SET lại KHÔNG atomic — hai Gateway worker cùng poll
đồng thời có thể đọc cùng bản QUEUED, cả hai append vào response, và write sau ghi đè write
trước (lost update, delivery_count sai, KHÔNG rõ ai thực sự "sở hữu" delivery attempt đó).

Giờ mỗi claim chạy qua MỘT Lua script (``_CLAIM_SCRIPT``, atomic — Redis chạy Lua single-
threaded, không round-trip giữa hai worker nào có thể xen giữa). Script kiểm tra + ghi trong
CÙNG MỘT operation: record tồn tại, chưa terminal, chưa expired, state claimable (QUEUED, hoặc
DELIVERED nhưng ``visibility_deadline`` đã qua), rồi tăng ``delivery_attempt``, sinh
``fencing_token`` mới (= ``{command_id}:{attempt}`` — duy nhất theo attempt, không cần random
vì attempt đơn điệu), set DELIVERED + ``visibility_deadline`` mới. Worker thua cuộc thấy
``visibility_deadline`` đã được worker thắng đẩy ra tương lai → tự reject (``still_visible``).

Mọi request Agent SAU delivery (accept/progress/terminal) phải gửi lại đúng
``delivery_attempt`` + ``fencing_token`` của lần claim mà nó nhận được. Sai (stale attempt sau
redelivery, token sai, hoặc version không khớp) → 409 với domain reason rõ ràng
(``stale_delivery_attempt``/``invalid_fencing_token``/``version_conflict``), KHÔNG silently
accept. Đây là delivery ownership fencing — KHÔNG phải execution idempotency (đã có riêng ở
``aoip.agent.idempotency.IdempotencyLedger`` + execution lease phía agent, không đổi ở đây).

Effectively-once, KHÔNG phải exactly-once tuyệt đối: hai delivery attempt liên tiếp cho cùng
command_id vẫn là CÙNG command identity; agent-side idempotency ledger là lớp chặn re-mutation
cuối cùng nếu attempt N+1 vẫn map về cùng execution key.

## Visibility heartbeat (long-running execution safety)

``visibility_deadline`` (60s mặc định) là RIÊNG BIỆT với execution lease TTL phía agent
(``aoip.agent.lease``, 120s mặc định) — hai timer độc lập. Một mutation chạy lâu hơn 60s mà
KHÔNG gia hạn visibility sẽ bị Gateway coi là "quá hạn xem thấy" và redeliver (attempt mới,
token mới) NGAY CẢ KHI agent vẫn đang chạy attempt cũ bình thường.

``POST /commands/heartbeat`` cho phép agent gia hạn ``visibility_deadline`` ĐỊNH KỲ trong lúc
RUNNING/RECONCILING — KHÔNG đổi ``delivery_attempt``, KHÔNG cấp ``fencing_token`` mới (đây là
gia hạn, không phải claim lại). Guard giống accept/progress: agent_id/attempt/token/version
phải khớp; chỉ hợp lệ khi state hiện tại là RUNNING hoặc RECONCILING (không heartbeat được
DELIVERED/ACCEPTED chưa vào RUNNING, và tuyệt đối không heartbeat được TERMINAL/EXPIRED).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aoip.protocol import (  # canonical state vocabulary — ADR-002
    PROGRESS_STATES as _PROGRESS,
    ST_ACCEPTED,
    ST_EXPIRED,
    ST_QUEUED,
    TERMINAL_STATES as TERMINAL,
)
from gateway.tenant_context import get_tenant_ctx, is_admin_ctx, require_agent_tenant
from services.agent_command_ledger import pg_record_enqueue, pg_record_terminal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/agent/rt", tags=["agent-runtime-delivery"])

_VISIBILITY_S = 60
_TTL_TERMINAL_S = 604800
_TTL_ACTIVE_S = 86400
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")

# KEYS[1]=rec_key KEYS[2]=ready_key
# ARGV[1]=command_id ARGV[2]=now ARGV[3]=visibility_s ARGV[4]=ttl_active_s ARGV[5]=ttl_terminal_s
_CLAIM_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  redis.call('ZREM', KEYS[2], ARGV[1])
  return cjson.encode({claimed=false, reason='missing'})
end
local rec = cjson.decode(raw)
local now = tonumber(ARGV[2])
local TERMINAL = {COMPLETED=true, FAILED=true, ESCALATED=true, EXPIRED=true}
if TERMINAL[rec.state] then
  redis.call('ZREM', KEYS[2], ARGV[1])
  return cjson.encode({claimed=false, reason='terminal'})
end
if rec.expires_at and now >= rec.expires_at then
  rec.state = 'EXPIRED'
  rec.terminal_at = now
  rec.outcome = {reason='expired_before_terminal'}
  rec.record_version = (rec.record_version or 0) + 1
  redis.call('SET', KEYS[1], cjson.encode(rec), 'EX', tonumber(ARGV[5]))
  redis.call('ZREM', KEYS[2], ARGV[1])
  return cjson.encode({claimed=false, reason='expired'})
end
if rec.state == 'QUEUED' then
  -- claimable
elseif rec.state == 'DELIVERED' then
  if rec.visibility_deadline and now < rec.visibility_deadline then
    return cjson.encode({claimed=false, reason='still_visible'})
  end
else
  return cjson.encode({claimed=false, reason='not_claimable_state'})
end
local attempt = (rec.delivery_attempt or rec.delivery_count or 0) + 1
rec.delivery_attempt = attempt
rec.delivery_count = attempt
rec.fencing_token = ARGV[1] .. ':' .. tostring(attempt)
rec.state = 'DELIVERED'
rec.last_delivered_at = now
rec.delivered_at = now
rec.visibility_deadline = now + tonumber(ARGV[3])
rec.record_version = (rec.record_version or 0) + 1
redis.call('SET', KEYS[1], cjson.encode(rec), 'EX', tonumber(ARGV[4]))
redis.call('ZADD', KEYS[2], rec.visibility_deadline, ARGV[1])
return cjson.encode({claimed=true, record=rec})
"""


def _rec_key(tenant: str, command_id: str) -> str:
    return f"omni:cmd:rec:{tenant}:{command_id}"


def _ready_key(tenant: str, agent_id: str) -> str:
    return f"omni:cmd:ready:{tenant}:{agent_id}"


def _get_pg_pool(request: Request) -> Any:
    """PG ledger pool (durability IT-6). None = degraded, reconciler backfill sau."""
    return getattr(request.app.state, "admin_pool", None)


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _tenant_of(request: Request, body_tenant: str) -> str:
    ctx = get_tenant_ctx(request)
    return body_tenant if is_admin_ctx(ctx) else (ctx.tenant_id if ctx else body_tenant)


_MUTATION_FLAG_KEY = "aoip_mutation_enabled"


def _master_auto_execute_enabled() -> bool:
    return os.getenv("OMNI_AUTO_EXECUTE_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _flag_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _enforce_mutation_toggle(request: Request, tenant: str) -> None:
    """Guard every durable recovery enqueue with both mutation permission gates."""
    repo = getattr(request.app.state, "admin_repo", None)
    if repo is None:
        # Lightweight ASGI/unit harnesses do not wire the control-plane repository.
        return
    try:
        requested = _flag_bool(await repo.get_runtime_flag(_MUTATION_FLAG_KEY, tenant_id=tenant))
    except Exception as exc:  # noqa: BLE001 - fail closed at the control-plane boundary
        logger.exception("mutation toggle lookup failed tenant=%s", tenant)
        raise HTTPException(status_code=503, detail="Mutation control unavailable") from exc
    if not requested:
        raise HTTPException(status_code=423, detail={
            "reason": "tenant_toggle_off", "tenant_id": tenant,
        })
    if not _master_auto_execute_enabled():
        raise HTTPException(status_code=423, detail={
            "reason": "master_kill_switch_off", "tenant_id": tenant,
        })


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
    delivery_attempt: int = Field(ge=1)
    fencing_token: str = Field(min_length=1, max_length=200)
    expected_version: int | None = Field(default=None, ge=0)


class AckProgress(AckDelivered):
    phase: str = Field(min_length=1, max_length=32)


class Terminal(AckDelivered):
    state: str = Field(min_length=1, max_length=32)
    outcome: dict = Field(default_factory=dict)


def _validate_typed_mutation_payload(payload: dict) -> None:
    """Typed capability payloads cannot enter durable mutation without proof gates.

    Legacy test/read models may still carry generic payloads; once a payload
    declares a capability, it is treated as a mutation contract and validated
    strictly here.
    """
    if "capability" not in payload:
        return
    required = ("capability_version", "target", "verification", "approval")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"typed mutation payload missing: {missing}")
    if not isinstance(payload.get("target"), dict) or not payload["target"]:
        raise HTTPException(status_code=422, detail="typed mutation target is required")
    if not isinstance(payload.get("verification"), dict):
        raise HTTPException(status_code=422, detail="typed mutation verification contract is required")


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
    await _enforce_mutation_toggle(request, tenant)
    _validate_typed_mutation_payload(body.payload)
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
        "delivery_attempt": 0, "fencing_token": "", "delivered_at": 0,
        "visibility_deadline": 0, "record_version": 0,
    }
    if now >= expires_at:
        base.update(state=ST_EXPIRED, terminal_at=now, outcome={"reason": "expired_before_delivery"})
        await redis.set(rkey, json.dumps(base), ex=_TTL_TERMINAL_S)
        await pg_record_terminal(_get_pg_pool(request), base)
        return JSONResponse(content={"command_id": body.command_id, "state": ST_EXPIRED})

    base["state"] = ST_QUEUED
    await redis.set(rkey, json.dumps(base), ex=_TTL_ACTIVE_S)
    await redis.zadd(_ready_key(tenant, body.agent_id), {body.command_id: now})
    await pg_record_enqueue(_get_pg_pool(request), base)
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
        claimed = await _claim(redis, tenant, agent_id, cid, pg_pool=_get_pg_pool(request))
        if claimed is not None:
            out.append(claimed)
        # not claimed (terminal/expired/still_visible/lost race) → skip silently,
        # another worker won this attempt or it is no longer claimable.
    return JSONResponse(content={"commands": out})


async def _claim(redis, tenant: str, agent_id: str, command_id: str,
                 pg_pool: Any = None) -> dict | None:
    """Atomic claim: một Lua round-trip, chỉ một caller thắng một delivery attempt.

    Trả record đã claim (state=DELIVERED, attempt/token mới) hoặc None nếu không claim được
    (record mất, terminal, expired, hoặc worker khác vừa thắng attempt này — ``still_visible``).
    """
    raw = await redis.eval(
        _CLAIM_SCRIPT, 2, _rec_key(tenant, command_id), _ready_key(tenant, agent_id),
        command_id, int(time.time()), _VISIBILITY_S, _TTL_ACTIVE_S, _TTL_TERMINAL_S)
    result = json.loads(raw)
    if not result.get("claimed"):
        reason = result.get("reason", "unknown")
        event = "expired_claim_rejected" if reason == "expired" else "claim_conflict"
        logger.info("agent_runtime.%s command_id=%s reason=%s", event, command_id, reason)
        if reason == "expired":
            # Lua vừa set EXPIRED terminal trong Redis — mirror sang PG ledger (IT-6)
            raw_rec = await redis.get(_rec_key(tenant, command_id))
            if raw_rec is not None:
                await pg_record_terminal(pg_pool, json.loads(raw_rec))
        return None
    record = result["record"]
    event = "redelivery" if record.get("delivery_attempt", 1) > 1 else "claim_success"
    logger.info("agent_runtime.%s command_id=%s attempt=%s", event, command_id,
               record.get("delivery_attempt"))
    return record


class _OwnershipConflict(Exception):
    """Fencing rejection — stale attempt/token/version. KHÔNG silently accept."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _check_fencing(rec: dict, *, agent_id: str, delivery_attempt: int, fencing_token: str,
                   expected_version: int | None) -> None:
    """Ownership guard — raise ``_OwnershipConflict`` nếu agent/attempt/token/version không
    khớp record hiện tại. Kiểm agent_id trước (isolation), rồi attempt (lỗi phổ biến nhất sau
    redelivery), rồi token, rồi version — thứ tự chỉ ảnh hưởng ``reason`` trả về."""
    if rec.get("agent_id") != agent_id:
        raise _OwnershipConflict("agent_mismatch")
    if rec.get("delivery_attempt", 0) != delivery_attempt:
        raise _OwnershipConflict("stale_delivery_attempt")
    if rec.get("fencing_token", "") != fencing_token:
        raise _OwnershipConflict("invalid_fencing_token")
    if expected_version is not None and rec.get("record_version", 0) != expected_version:
        raise _OwnershipConflict("version_conflict")


async def _advance(redis, tenant: str, command_id: str, agent_id: str, target: str, *,
                   delivery_attempt: int, fencing_token: str,
                   expected_version: int | None) -> dict | None:
    """Ownership-guarded transition. Idempotent nếu record ĐÃ ở ``target`` với đúng
    attempt/token (retry an toàn) — không bump version thêm lần nữa."""
    raw = await redis.get(_rec_key(tenant, command_id))
    if raw is None:
        return None
    rec = json.loads(raw)
    if rec["state"] in TERMINAL:
        return rec
    _check_fencing(rec, agent_id=agent_id, delivery_attempt=delivery_attempt,
                   fencing_token=fencing_token, expected_version=expected_version)
    if rec["state"] == target:
        return rec  # idempotent retry của cùng attempt/token đã thành công trước đó
    now = int(time.time())
    rec["state"] = target
    rec["record_version"] = rec.get("record_version", 0) + 1
    await redis.set(_rec_key(tenant, command_id), json.dumps(rec), ex=_TTL_ACTIVE_S)
    await redis.zadd(_ready_key(tenant, agent_id), {command_id: now + _VISIBILITY_S})
    return rec


def _conflict_response(command_id: str, exc: _OwnershipConflict) -> JSONResponse:
    logger.info("agent_runtime.ownership_conflict command_id=%s reason=%s", command_id, exc.reason)
    return JSONResponse(status_code=409,
                        content={"command_id": command_id, "error": exc.reason})


@router.post("/commands/accept")
async def accept_command(body: AckDelivered, request: Request) -> JSONResponse:
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    tenant = _tenant_of(request, body.tenant_id)
    try:
        rec = await _advance(redis, tenant, body.command_id, body.agent_id, ST_ACCEPTED,
                             delivery_attempt=body.delivery_attempt,
                             fencing_token=body.fencing_token,
                             expected_version=body.expected_version)
    except _OwnershipConflict as exc:
        return _conflict_response(body.command_id, exc)
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
    try:
        rec = await _advance(redis, tenant, body.command_id, body.agent_id, body.phase,
                             delivery_attempt=body.delivery_attempt,
                             fencing_token=body.fencing_token,
                             expected_version=body.expected_version)
    except _OwnershipConflict as exc:
        return _conflict_response(body.command_id, exc)
    if rec is None:
        raise HTTPException(status_code=404, detail="command not found")
    return JSONResponse(content={"command_id": body.command_id, "state": rec["state"]})


@router.post("/commands/heartbeat")
async def heartbeat_command(body: AckDelivered, request: Request) -> JSONResponse:
    """Gia hạn ``visibility_deadline`` trong lúc RUNNING/RECONCILING — KHÔNG đổi
    ``delivery_attempt``, KHÔNG cấp ``fencing_token`` mới, KHÔNG revive terminal/expired."""
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    tenant = _tenant_of(request, body.tenant_id)
    raw = await redis.get(_rec_key(tenant, body.command_id))
    if raw is None:
        raise HTTPException(status_code=404, detail="command not found")
    rec = json.loads(raw)
    now = int(time.time())
    if rec["state"] in TERMINAL:
        logger.info("agent_runtime.visibility_heartbeat_failed command_id=%s reason=terminal",
                   body.command_id)
        return JSONResponse(status_code=409, content={
            "command_id": body.command_id, "error": "terminal_no_heartbeat"})
    if rec.get("expires_at") and now >= rec["expires_at"]:
        rec.update(state=ST_EXPIRED, terminal_at=now, outcome={"reason": "expired_before_terminal"})
        rec["record_version"] = rec.get("record_version", 0) + 1
        await redis.set(_rec_key(tenant, body.command_id), json.dumps(rec), ex=_TTL_TERMINAL_S)
        await redis.zrem(_ready_key(tenant, body.agent_id), body.command_id)
        await pg_record_terminal(_get_pg_pool(request), rec)
        logger.info("agent_runtime.visibility_heartbeat_failed command_id=%s reason=expired",
                   body.command_id)
        return JSONResponse(status_code=409, content={
            "command_id": body.command_id, "error": "expired"})
    try:
        _check_fencing(rec, agent_id=body.agent_id, delivery_attempt=body.delivery_attempt,
                       fencing_token=body.fencing_token, expected_version=body.expected_version)
    except _OwnershipConflict as exc:
        logger.info("agent_runtime.visibility_heartbeat_failed command_id=%s reason=%s",
                   body.command_id, exc.reason)
        return _conflict_response(body.command_id, exc)
    if rec["state"] not in _PROGRESS:
        logger.info("agent_runtime.visibility_heartbeat_failed command_id=%s reason=not_running "
                   "state=%s", body.command_id, rec["state"])
        return JSONResponse(status_code=409, content={
            "command_id": body.command_id, "error": "not_running", "state": rec["state"]})
    new_deadline = now + _VISIBILITY_S
    rec["visibility_deadline"] = new_deadline
    rec["record_version"] = rec.get("record_version", 0) + 1
    await redis.set(_rec_key(tenant, body.command_id), json.dumps(rec), ex=_TTL_ACTIVE_S)
    await redis.zadd(_ready_key(tenant, body.agent_id), {body.command_id: new_deadline})
    logger.info("agent_runtime.delivery_visibility_extended command_id=%s deadline=%s",
               body.command_id, new_deadline)
    return JSONResponse(content={"command_id": body.command_id, "visibility_deadline": new_deadline,
                                 "record_version": rec["record_version"]})


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
        if rec["state"] == body.state and rec["outcome"] == body.outcome:
            await redis.zrem(_ready_key(tenant, body.agent_id), body.command_id)
            return JSONResponse(content={"acknowledged": True, "command_id": body.command_id,
                                         "state": rec["state"], "idempotent": True})
        logger.info("agent_runtime.ownership_conflict command_id=%s reason=terminal_outcome_conflict",
                   body.command_id)
        return JSONResponse(status_code=409, content={
            "command_id": body.command_id, "error": "terminal_outcome_conflict",
            "state": rec["state"]})
    try:
        _check_fencing(rec, agent_id=body.agent_id, delivery_attempt=body.delivery_attempt,
                       fencing_token=body.fencing_token, expected_version=body.expected_version)
    except _OwnershipConflict as exc:
        return _conflict_response(body.command_id, exc)
    rec.update(state=body.state, terminal_at=now, outcome=body.outcome)
    rec["record_version"] = rec.get("record_version", 0) + 1
    await redis.set(_rec_key(tenant, body.command_id), json.dumps(rec), ex=_TTL_TERMINAL_S)
    await redis.zrem(_ready_key(tenant, body.agent_id), body.command_id)
    # PG ledger (IT-6): best-effort — Redis đã durable, reconciler backfill nếu PG down.
    pg_result = await pg_record_terminal(_get_pg_pool(request), rec)
    logger.info("agent_runtime.terminal_pg_ledger command_id=%s result=%s",
               body.command_id, pg_result)
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
