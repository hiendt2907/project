"""Autonomy policy management endpoints — read/write fine-grained autonomy rules."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pkg.autonomy.policy import AutonomyPolicyStore, PolicyRule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autonomy", tags=["autonomy"])

_store = AutonomyPolicyStore()

_VALID_TIERS = ("shadow", "assist", "auto")
_TIER_RANK = {"shadow": 0, "assist": 1, "auto": 2}
# Readiness do worker tính & ghi vào key này (gateway KHÔNG import workers — bất biến).
_READINESS_KEY = "omni:tier:readiness:{tenant}"


class TierChangeRequest(BaseModel):
    tier: str = Field(..., description="shadow|assist|auto")
    tenant_id: str = "default"
    actor: str = "admin_ui"
    forced: bool = Field(default=False, description="Nâng tier khi readiness chưa đạt (2-step confirm)")
    confirm: bool = Field(default=False, description="Bắt buộc true khi NÂNG tier (≥ tier hiện tại)")


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


@router.get("/policy")
async def get_policy(request: Request) -> JSONResponse:
    """Return the current autonomy policy list as JSON (ordered; first match wins)."""
    redis = _get_redis(request)
    try:
        rules = await _store.get_policy(redis)
        return JSONResponse(content={"policy": [r.model_dump() for r in rules]})
    except Exception as exc:
        logger.error("autonomy.get_policy error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/policy/rule")
async def add_policy_rule(request: Request, rule: PolicyRule) -> JSONResponse:
    """Prepend a new rule to the policy list. The new rule takes highest priority."""
    redis = _get_redis(request)
    try:
        await _store.set_rule(redis, rule)
        return JSONResponse(content={"status": "ok", "rule": rule.model_dump()})
    except Exception as exc:
        logger.error("autonomy.add_policy_rule error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/policy/history")
async def get_policy_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    """Return the policy change history (most recent first)."""
    redis = _get_redis(request)
    try:
        history = await _store.get_history(redis, limit=limit)
        return JSONResponse(content={"history": history})
    except Exception as exc:
        logger.error("autonomy.get_policy_history error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/policy/reset")
async def reset_policy(request: Request) -> JSONResponse:
    """Reset policy to built-in defaults. Requires API key (enforced at router level)."""
    redis = _get_redis(request)
    try:
        await _store.reset_to_defaults(redis)
        rules = await _store.get_policy(redis)
        return JSONResponse(
            content={
                "status": "reset",
                "policy": [r.model_dump() for r in rules],
            }
        )
    except Exception as exc:
        logger.error("autonomy.reset_policy error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Autonomy tier (MASTER_PLAN §6/§8 step 6) ───────────────────────────────────
def _get_admin_repo(request: Request) -> Any:
    repo = getattr(request.app.state, "admin_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail="Admin config store offline (OMNI_ADMIN_PG_DSN chưa cấu hình)",
        )
    return repo


@router.get("/tier")
async def get_tier(request: Request, tenant_id: str = Query(default="default")) -> JSONResponse:
    """Tier hiệu lực hiện tại (source-of-truth Postgres omni_admin)."""
    repo = _get_admin_repo(request)
    tier = await repo.get_tier(tenant_id) or "shadow"
    return JSONResponse(content={"tier": tier, "tenant_id": tenant_id})


@router.post("/tier")
async def set_tier(request: Request, body: TierChangeRequest) -> JSONResponse:
    """Đổi tier — atomic 1 TX (UPSERT tier + config_change_log + crat_outbox).

    NÂNG tier (≥ hiện tại) yêu cầu ``confirm=true`` (2-step). CRAT ``AUTONOMY_TIER_CHANGED``
    do outbox drainer ghi. KHÔNG tự nhảy tier — chỉ operator gọi endpoint này.
    """
    if body.tier not in _VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"tier không hợp lệ: {body.tier}")
    repo = _get_admin_repo(request)
    current = await repo.get_tier(body.tenant_id) or "shadow"
    is_promotion = _TIER_RANK[body.tier] > _TIER_RANK[current]
    if is_promotion and not body.confirm:
        raise HTTPException(
            status_code=409,
            detail=f"Nâng tier {current}→{body.tier} cần confirm=true (2-step)",
        )
    # readiness snapshot (nếu worker đã ghi) — đính kèm history.
    readiness: dict[str, Any] = {}
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            raw = await redis.get(_READINESS_KEY.format(tenant=body.tenant_id))
            if raw:
                readiness = json.loads(raw)
        except Exception:  # noqa: BLE001
            readiness = {}
    try:
        result = await repo.set_tier(
            tier=body.tier, actor=body.actor, tenant_id=body.tenant_id,
            readiness=readiness, forced=body.forced,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("autonomy.set_tier error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(content={
        "status": "ok", "from": current, "to": body.tier,
        "promotion": is_promotion, **result,
    })


@router.get("/readiness")
async def get_readiness(request: Request, tenant_id: str = Query(default="default")) -> JSONResponse:
    """Readiness do worker tính (đọc Redis). CHỈ hiển thị — không tự nhảy tier."""
    redis = _get_redis(request)
    try:
        raw = await redis.get(_READINESS_KEY.format(tenant=tenant_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not raw:
        return JSONResponse(content={"readiness": None, "tenant_id": tenant_id})
    return JSONResponse(content={"readiness": json.loads(raw), "tenant_id": tenant_id})


# ── Risk-Class Matrix (MASTER_PLAN §2/§6.7) ────────────────────────────────────
class RiskClassRequest(BaseModel):
    tool_name: str
    risk_class: str = Field(..., description="READONLY|LOW|MEDIUM|HIGH")
    reason: str | None = None
    tenant_id: str = "default"
    actor: str = "admin_ui"
    confirm: bool = Field(default=False, description="Bắt buộc true khi HẠ rủi ro (2-step)")


_RISK_RANK = {"READONLY": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


@router.get("/risk-class")
async def get_risk_class(request: Request, tenant_id: str = Query(default="default")) -> JSONResponse:
    """Bảng tĩnh §2 ghép override DB. Mỗi tool: effective + static + override + dangerous-lock."""
    from pkg.risk_taxonomy import DANGEROUS_TOOLS, STATIC_RISK_CLASS

    repo = _get_admin_repo(request)
    overrides = await repo.list_risk_class_overrides(tenant_id)
    tools = []
    for tool_name in sorted(STATIC_RISK_CLASS):
        static = STATIC_RISK_CLASS[tool_name]
        ov = overrides.get(tool_name)
        tools.append({
            "tool_name": tool_name,
            "static_risk_class": static,
            "override": ov["risk_class"] if ov else None,
            "effective": ov["risk_class"] if ov else static,
            "dangerous_locked": tool_name in DANGEROUS_TOOLS,
            "reason": ov["reason"] if ov else None,
        })
    return JSONResponse(content={"tools": tools, "tenant_id": tenant_id})


@router.post("/risk-class")
async def set_risk_class(request: Request, body: RiskClassRequest) -> JSONResponse:
    """Override risk-class 1 tool. Hạ rủi ro (override < static) cần confirm=true."""
    from pkg.risk_taxonomy import STATIC_RISK_CLASS

    if body.risk_class not in _RISK_RANK:
        raise HTTPException(status_code=400, detail=f"risk_class không hợp lệ: {body.risk_class}")
    repo = _get_admin_repo(request)
    static = STATIC_RISK_CLASS.get(body.tool_name, "HIGH")
    is_downgrade = _RISK_RANK[body.risk_class] < _RISK_RANK.get(static, 3)
    if is_downgrade and not body.confirm:
        raise HTTPException(
            status_code=409,
            detail=f"Hạ rủi ro {body.tool_name} {static}→{body.risk_class} cần confirm=true (2-step)",
        )
    try:
        result = await repo.set_risk_class_override(
            tool_name=body.tool_name, risk_class=body.risk_class,
            reason=body.reason, actor=body.actor, tenant_id=body.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("autonomy.set_risk_class error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(content={"status": "ok", "downgrade": is_downgrade, **result})


# ── Runtime Flags (MASTER_PLAN §6.7) ───────────────────────────────────────────
class RuntimeFlagRequest(BaseModel):
    flag_key: str
    flag_value: Any
    value_type: str = Field(..., description="int|bool|str|float|json")
    tenant_id: str = "default"
    actor: str = "admin_ui"


@router.get("/flags")
async def get_flags(request: Request, tenant_id: str = Query(default="default")) -> JSONResponse:
    repo = _get_admin_repo(request)
    flags = await repo.list_runtime_flags(tenant_id)
    return JSONResponse(content={"flags": flags, "tenant_id": tenant_id})


@router.post("/flags")
async def set_flag(request: Request, body: RuntimeFlagRequest) -> JSONResponse:
    if body.value_type not in ("int", "bool", "str", "float", "json"):
        raise HTTPException(status_code=400, detail=f"value_type không hợp lệ: {body.value_type}")
    repo = _get_admin_repo(request)
    try:
        result = await repo.set_runtime_flag(
            flag_key=body.flag_key, flag_value=body.flag_value,
            value_type=body.value_type, actor=body.actor, tenant_id=body.tenant_id,
        )
    except Exception as exc:
        logger.error("autonomy.set_flag error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(content={"status": "ok", **result})


# ── Tenant / API keys (MASTER_PLAN §6.7) ───────────────────────────────────────
class TenantRequest(BaseModel):
    tenant_id: str
    display_name: str
    actor: str = "admin_ui"


class TenantStatusRequest(BaseModel):
    status: str = Field(..., description="active|suspended")
    actor: str = "admin_ui"


class ApiKeyRequest(BaseModel):
    label: str | None = None
    actor: str = "admin_ui"


@router.get("/tenants")
async def get_tenants(request: Request) -> JSONResponse:
    repo = _get_admin_repo(request)
    return JSONResponse(content={"tenants": await repo.list_tenants()})


@router.post("/tenants")
async def create_tenant(request: Request, body: TenantRequest) -> JSONResponse:
    repo = _get_admin_repo(request)
    try:
        result = await repo.create_tenant(
            tenant_id=body.tenant_id, display_name=body.display_name, actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("autonomy.create_tenant error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(content={"status": "ok", **result})


@router.post("/tenants/{tenant_id}/status")
async def set_tenant_status(request: Request, tenant_id: str, body: TenantStatusRequest) -> JSONResponse:
    repo = _get_admin_repo(request)
    try:
        result = await repo.set_tenant_status(
            tenant_id=tenant_id, status=body.status, actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(content={"status": "ok", **result})


@router.get("/tenants/{tenant_id}/api-keys")
async def get_api_keys(request: Request, tenant_id: str) -> JSONResponse:
    repo = _get_admin_repo(request)
    return JSONResponse(content={"api_keys": await repo.list_api_keys(tenant_id)})


@router.post("/tenants/{tenant_id}/api-keys")
async def create_api_key(request: Request, tenant_id: str, body: ApiKeyRequest) -> JSONResponse:
    """Sinh key ngẫu nhiên, lưu HASH (sha256), trả plaintext MỘT LẦN duy nhất."""
    import hashlib
    import secrets

    repo = _get_admin_repo(request)
    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:8]
    try:
        result = await repo.create_api_key(
            tenant_id=tenant_id, key_hash=key_hash, key_prefix=key_prefix,
            actor=body.actor, label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("autonomy.create_api_key error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # plaintext CHỈ trả lần này — không bao giờ lưu/hiển thị lại.
    return JSONResponse(content={"status": "ok", "api_key": raw_key, **result})


@router.delete("/tenants/{tenant_id}/api-keys/{key_id}")
async def revoke_api_key(request: Request, tenant_id: str, key_id: int) -> JSONResponse:
    repo = _get_admin_repo(request)
    try:
        result = await repo.revoke_api_key(key_id=key_id, actor="admin_ui", tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(content={"status": "ok", **result})


# ── HITL Queue (MASTER_PLAN §4/§6.7) — duyệt UI song song Telegram ──────────────
class HitlDecideRequest(BaseModel):
    decision: str = Field(..., description="APPROVED|REJECTED")
    actor: str = "admin_ui"
    tenant_id: str = "default"


_HITL_DECISIONS_TOPIC = "omni-hitl-decisions"


@router.get("/hitl/pending")
async def get_hitl_pending(request: Request, tenant_id: str = Query(default="default")) -> JSONResponse:
    repo = _get_admin_repo(request)
    return JSONResponse(content={"pending": await repo.list_hitl_pending(tenant_id)})


@router.post("/hitl/{pending_id}/decide")
async def decide_hitl(request: Request, pending_id: str, body: HitlDecideRequest) -> JSONResponse:
    """Duyệt HITL trên UI. Atomic ledger+outbox(HITL_DECISION) TRƯỚC, rồi publish Kafka
    để worker định tuyến APPROVED→omni-actions / REJECTED→omni-action-feedback."""
    if body.decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail=f"decision không hợp lệ: {body.decision}")
    repo = _get_admin_repo(request)
    try:
        result = await repo.decide_hitl(
            pending_id=pending_id, decision=body.decision,
            actor=body.actor, channel="ui", tenant_id=body.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("autonomy.decide_hitl error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Publish quyết định cho worker (CRAT-intent đã durable trong outbox ở trên).
    kafka = getattr(request.app.state, "kafka", None)
    if kafka is not None:
        try:
            envelope = json.dumps({
                "pending_id": pending_id, "decision": body.decision,
                "tool_name": result.get("tool_name"), "actor": body.actor,
                "tenant_id": body.tenant_id, "channel": "ui",
            }).encode()
            await kafka.send_and_wait(_HITL_DECISIONS_TOPIC, value=envelope,
                                      key=pending_id.encode())
        except Exception as exc:  # noqa: BLE001
            logger.warning("autonomy.decide_hitl kafka publish failed: %s", exc)
    return JSONResponse(content={"status": "ok", **result})
