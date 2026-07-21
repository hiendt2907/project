"""Autonomy policy management endpoints — read/write fine-grained autonomy rules."""

from __future__ import annotations

import json
import logging
import os
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
    """Tier HIỆU LỰC THẬT (cache → Postgres → env-derive) — cùng resolve_tier()
    mà pkg.autonomy.tier_gate dùng để gate mutation thật.

    Phase 2 fix (0-6 roadmap): trước đây endpoint này đọc thẳng
    ``repo.get_tier(tenant_id) or "shadow"`` — bỏ qua Redis cache VÀ bỏ qua
    env-derive fallback từ OMNI_AUTO_EXECUTE_ENABLED. Khi PG chưa có row
    tường minh cho tenant (phổ biến — không phải mọi tenant đều được set tier
    tay), operator nhìn thấy "shadow" nhưng gate thật (resolve_tier, đọc bởi
    cả K8s lane lẫn VM recovery lane từ Phase 2) có thể đang trả "auto" (dẫn
    xuất từ kill-switch legacy) — hai giá trị lệch nhau, operator bị đánh lừa
    về mức độ tự động thật sự đang hiệu lực. Caught live 2026-07-21: GET
    /autonomy/tier trả "shadow" ngay trước một drill mà chính sách hiệu lực
    thật lại là "auto".
    """
    from types import SimpleNamespace

    from pkg.autonomy.tier_gate import resolve_tier

    repo = _get_admin_repo(request)
    redis = getattr(request.app.state, "redis", None)
    master_enabled = os.getenv("OMNI_AUTO_EXECUTE_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    settings = SimpleNamespace(
        omni_autonomy_tier=os.getenv("OMNI_AUTONOMY_TIER", ""),
        omni_auto_execute_enabled=master_enabled,
    )
    tier = await resolve_tier(settings=settings, repo=repo, redis=redis, tenant_id=tenant_id)
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


MUTATION_FLAG_KEY = "aoip_mutation_enabled"


class MutationToggleRequest(BaseModel):
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    enabled: bool
    actor: str = Field(default="admin_ui", min_length=1, max_length=128)
    confirm: bool = False


def _master_auto_execute_enabled() -> bool:
    return os.getenv("OMNI_AUTO_EXECUTE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _flag_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _mutation_status(*, requested: bool, master_enabled: bool) -> tuple[bool, str]:
    if not requested:
        return False, "tenant_toggle_off"
    if not master_enabled:
        return False, "master_kill_switch_off"
    return True, "enabled"


@router.get("/mutation")
async def get_mutation_toggle(request: Request, tenant_id: str = Query(default="default")) -> JSONResponse:
    """Read the tenant mutation switch and its effective fail-closed state."""
    repo = _get_admin_repo(request)
    raw = await repo.get_runtime_flag(MUTATION_FLAG_KEY, tenant_id=tenant_id)
    requested = _flag_bool(raw)
    master_enabled = _master_auto_execute_enabled()
    effective, reason = _mutation_status(requested=requested, master_enabled=master_enabled)
    return JSONResponse(content={
        "tenant_id": tenant_id,
        "requested": requested,
        "master_kill_switch": master_enabled,
        "effective": effective,
        "reason": reason,
        "flag_key": MUTATION_FLAG_KEY,
    })


@router.post("/mutation")
async def set_mutation_toggle(request: Request, body: MutationToggleRequest) -> JSONResponse:
    """Set the tenant mutation switch; every change is persisted/audited.

    Enabling requires a second confirmation. The tenant switch can never
    override the process-wide master kill switch.
    """
    repo = _get_admin_repo(request)
    current = _flag_bool(await repo.get_runtime_flag(MUTATION_FLAG_KEY, tenant_id=body.tenant_id))
    if body.enabled and not current and not body.confirm:
        raise HTTPException(status_code=409, detail="enabling mutation requires confirm=true")
    try:
        result = await repo.set_runtime_flag(
            flag_key=MUTATION_FLAG_KEY, flag_value=body.enabled, value_type="bool",
            actor=body.actor, tenant_id=body.tenant_id,
        )
    except Exception as exc:
        logger.error("autonomy.set_mutation_toggle error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    effective, reason = _mutation_status(
        requested=body.enabled, master_enabled=_master_auto_execute_enabled(),
    )
    return JSONResponse(content={
        "status": "ok", "tenant_id": body.tenant_id, "requested": body.enabled,
        "effective": effective, "reason": reason, "flag_key": MUTATION_FLAG_KEY,
        **result,
    })


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


class EnvironmentRequest(BaseModel):
    environment_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)
    environment_type: str = Field(..., description="production|staging|development")
    actor: str = "admin_ui"


class EnvironmentStatusRequest(BaseModel):
    status: str = Field(..., description="onboarding|active|suspended|archived")
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


@router.get("/tenants/{tenant_id}/environments")
async def get_environments(request: Request, tenant_id: str) -> JSONResponse:
    repo = _get_admin_repo(request)
    return JSONResponse(content={"environments": await repo.list_environments(tenant_id)})


@router.post("/tenants/{tenant_id}/environments")
async def create_environment(
    request: Request, tenant_id: str, body: EnvironmentRequest,
) -> JSONResponse:
    _require_admin_ctx(request)
    repo = _get_admin_repo(request)
    try:
        result = await repo.create_environment(
            tenant_id=tenant_id, environment_id=body.environment_id,
            display_name=body.display_name, environment_type=body.environment_type,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(content={"status": "ok", **result})


@router.post("/tenants/{tenant_id}/environments/{environment_id}/status")
async def set_environment_status(
    request: Request, tenant_id: str, environment_id: str,
    body: EnvironmentStatusRequest,
) -> JSONResponse:
    _require_admin_ctx(request)
    repo = _get_admin_repo(request)
    try:
        result = await repo.set_environment_status(
            tenant_id=tenant_id, environment_id=environment_id,
            status=body.status, actor=body.actor,
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


# ── Agent enrollment (IT-3) — one-time token + per-agent credential ────────────
def _require_admin_ctx(request: Request) -> None:
    """Enroll-token/revoke là thao tác provisioning (Admin API theo plan IT-3).
    Per-agent credential từ VM khách cũng pass _require_api_key (is_admin=False)
    — phải chặn ở đây để agent bị lộ key không tự phát token/revoke lẫn nhau."""
    from gateway.tenant_context import get_tenant_ctx, is_admin_ctx

    if not is_admin_ctx(get_tenant_ctx(request)):
        raise HTTPException(status_code=403, detail="Admin API key required")


class EnrollTokenRequest(BaseModel):
    label: str | None = None
    actor: str = "admin_ui"
    ttl_seconds: int | None = Field(
        default=None, ge=60, description="Hạn dùng token (giây); None = không hết hạn (lab)",
    )
    environment_id: str | None = Field(default=None, max_length=128)


@router.post("/tenants/{tenant_id}/enroll-tokens")
async def create_enroll_token(
    request: Request, tenant_id: str, body: EnrollTokenRequest,
) -> JSONResponse:
    """Phát one-time enroll token. Plaintext trả đúng MỘT lần — PG chỉ lưu sha256."""
    _require_admin_ctx(request)
    import hashlib
    import secrets
    from datetime import datetime, timedelta, timezone

    repo = _get_admin_repo(request)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=body.ttl_seconds)
        if body.ttl_seconds else None
    )
    try:
        result = await repo.create_enroll_token(
            tenant_id=tenant_id, token_hash=token_hash, token_prefix=raw_token[:8],
            actor=body.actor, label=body.label, expires_at=expires_at,
            environment_id=body.environment_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # plaintext CHỈ trả lần này.
    return JSONResponse(content={"status": "ok", "enroll_token": raw_token, **result})


@router.get("/tenants/{tenant_id}/agent-credentials")
async def get_agent_credentials(request: Request, tenant_id: str) -> JSONResponse:
    repo = _get_admin_repo(request)
    return JSONResponse(
        content={"agent_credentials": await repo.list_agent_credentials(tenant_id)},
    )


@router.delete("/tenants/{tenant_id}/agent-credentials/{agent_id}")
async def revoke_agent_credentials(
    request: Request, tenant_id: str, agent_id: str,
) -> JSONResponse:
    """Revoke mọi credential active của agent + xoá auth-cache → 401 tức thì."""
    _require_admin_ctx(request)
    repo = _get_admin_repo(request)
    revoked_hashes = await repo.revoke_agent_credentials(
        tenant_id=tenant_id, agent_id=agent_id, actor="admin_ui",
    )
    redis = getattr(request.app.state, "redis", None)
    if redis is not None and revoked_hashes:
        try:
            await redis.delete(*[f"omni:agentcred:cache:{h}" for h in revoked_hashes])
        except Exception:
            logger.warning("autonomy.revoke_agent_credentials: cache DEL failed", exc_info=True)
    return JSONResponse(content={"status": "ok", "revoked": len(revoked_hashes)})


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
