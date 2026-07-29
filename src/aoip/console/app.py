"""Hai portal production TÁCH BIỆT — Provider và Customer/Tenant.

Chung nguồn sự thật (RuntimeTrace/Mission/Audit), RIÊNG: read-model, namespace API (/v1),
permission, navigation, chính sách phơi bày. Danh tính từ SESSION server-side (identity.py),
KHÔNG từ client, KHÔNG từ agent API key.

  - /api/provider/v1/*  — vận hành nền tảng across-tenants (provider principal).
  - /api/tenant/v1/*    — chỉ tenant thuộc membership của principal.

Ranh giới cứng: READ-ONLY cho slice này (trừ approve → bounded Approval thật, Slice 2).
Ẩn menu KHÔNG phải authz — mọi route enforce ở backend. Từ chối = audit.
"""
from __future__ import annotations

import os
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from aoip.console import assets, identity, identity_store, oidc
from aoip.console.agents import build_provider_agents
from aoip.console.audit import build_provider_audit
from aoip.console.authz import (
    KIND_PROVIDER, KIND_TENANT, P_CHANGE_POLICY, P_RAW_EVIDENCE, P_VIEW, Principal,
)
from aoip.console.human_inbox import build_provider_human_inbox
from aoip.console.lab_incidents import create_lab_incident, list_provider_lab_incidents
from aoip.console.overview import build_provider_overview
from aoip.console.projections import provider_incident, tenant_incident
from aoip.console.settings import (
    build_provider_settings, issue_enroll_token, revoke_agent_credential,
)
from aoip.console.understanding import build_provider_understanding
from aoip.mission_store import MissionStore
from aoip.question_lifecycle import submit_answer
from aoip.agent.trace import RuntimeTrace

# Cookie session HOST-SCOPED, tên RIÊNG mỗi portal → không đụng nhau trên cùng trình duyệt
# (không cần ẩn danh). Provider và Tenant chạy khác host nên cookie vốn đã tách theo host;
# tên khác nhau là lớp phòng vệ thứ hai (tránh nhầm khi cùng domain gốc).
PROVIDER_COOKIE = "aoip_provider_session"
TENANT_COOKIE = "aoip_tenant_session"
SESSION_COOKIE = PROVIDER_COOKIE  # backward-compat cho import cũ (provider app)


class AnswerQuestionBody(BaseModel):
    value: str = Field(..., min_length=1, max_length=500)
    answered_by: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class EnrollTokenBody(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=120)
    ttl_seconds: int | None = Field(default=None, ge=60)
    environment_id: str | None = Field(default=None, max_length=128)


class CreateLabIncidentBody(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=128)
    agent_id: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=256)
    service: str = Field(..., min_length=1, max_length=128)
    unit: str = Field(..., min_length=1, max_length=128)


class CreateEnvironmentBody(BaseModel):
    environment_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)
    environment_type: str = Field(..., description="production|staging|development")


class CreateTenantBody(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)


class EnvironmentStatusBody(BaseModel):
    status: str = Field(..., description="onboarding|active|suspended|archived")


class AutonomyTierBody(BaseModel):
    tier: str = Field(..., description="shadow|assist|auto")
    confirm: bool = False
    forced: bool = False


class TenantPlanBody(BaseModel):
    plan_code: str = Field(..., min_length=1, max_length=64)
    agent_limit: int = Field(..., ge=0, le=1_000_000)
    autonomy_ceiling: str = Field(..., description="shadow|assist|auto")
    retention_days: int = Field(..., ge=1, le=3650)
    support_tier: str = Field(default="standard", description="standard|premium|enterprise")
    enabled: bool = True


class ScopeDecisionBody(BaseModel):
    """Phán quyết của admin tenant trên một đơn xin quyền.

    KHÔNG có trường ``tenant_id``: tenant suy ra từ session principal và đi vào
    mệnh đề WHERE của câu UPDATE. Một ``request_id`` đoán được vẫn không chạm
    sang tenant khác.
    """

    decision: str = Field(..., description="APPROVED|REJECTED")
    note: str = Field(default="", max_length=1000)
    cooldown_days: int = Field(default=14, ge=0, le=365)


async def _default_http_json(method: str, url: str, *, data=None, auth=None) -> dict:
    """Real token/JWKS fetch. Injectable ở test để chạy offline."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await (c.post(url, data=data, auth=auth) if method == "POST" else c.get(url))
        resp.raise_for_status()
        return resp.json()


def _wire_oidc(app, redis, *, kind: str, cfg_prefix: str, cookie_name: str,
               tenant_hint=None, http_json=None):
    """Gắn /auth/login + /auth/callback (Authorization Code + PKCE) vào app.

    http_json: coroutine(method,url,data,auth)->dict — inject để test offline.
    """
    import os
    from fastapi import Query
    from fastapi.responses import RedirectResponse

    fetch = http_json or _default_http_json

    @app.get("/auth/login")
    async def login(tenant: str | None = Query(default=None)):
        cfg = oidc.OIDCConfig.from_env(cfg_prefix)
        url = await oidc.begin_login(redis, cfg, kind=kind, tenant=tenant or tenant_hint,
                                     state_seed=os.urandom(24), verifier_seed=os.urandom(32))
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/callback")
    async def callback(code: str = Query(...), state: str = Query(...)):
        now = time.time()
        cfg = oidc.OIDCConfig.from_env(cfg_prefix)
        flow = await oidc.consume_flow(redis, state)
        if flow is None:
            raise HTTPException(400, "invalid or expired state")
        tokens = await fetch("POST", cfg.token_url, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id, "code_verifier": flow["verifier"],
        }, auth=(cfg.client_id, cfg.client_secret))
        jwks = await fetch("GET", cfg.jwks_url)
        claims = oidc.verify_id_token(tokens["id_token"], jwks, cfg, nonce=flow["nonce"], now=now)
        # Join provider-neutral: `email` (claim chuẩn) là khoá tới internal user; fallback `sub`.
        # Internal subject KHÔNG phải tenant/role — chỉ định danh; authz suy ra server-side.
        subject = (claims.get("email") or claims["sub"]).lower()
        if kind == KIND_PROVIDER:
            p = await identity.resolve_provider_principal(redis, subject)
        else:
            # Active org LUÔN validate server-side từ membership. Client có thể yêu cầu
            # 1 tenant (?tenant=) nhưng chỉ được cấp nếu là member; không yêu cầu →
            # mặc định membership (đơn) hoặc org đầu (đa) — không tin client chọn.
            memberships = await identity.list_memberships(redis, subject)
            requested = flow.get("tenant") or ""
            if requested:
                tenant = requested if requested in memberships else None
            else:
                tenant = sorted(memberships)[0] if memberships else None
            p = (await identity.resolve_tenant_principal(redis, subject, tenant)
                 if tenant else None)
        if p is None:
            await identity.audit(redis, event="DENIED", subject=subject,
                                 detail=f"login {kind}: no role/membership", ts=now)
            raise HTTPException(403, "no authorized role for this portal")
        sess = await identity.issue_session(redis, principal=p, now=now)
        secure = cfg.redirect_uri.startswith("https://")
        resp = RedirectResponse(os.environ.get("AOIP_POST_LOGIN_REDIRECT", "/"),
                                status_code=302)
        resp.set_cookie(cookie_name, sess.sid, httponly=True, secure=secure,
                        samesite="lax", max_age=identity.SESSION_TTL_S,
                        path=os.environ.get("AOIP_SESSION_COOKIE_PATH", "/"))
        return resp


def _origins(env_key: str) -> list[str]:
    """Origin allow-list cho CSRF check (comma-separated env). Rỗng = chỉ same-origin."""
    import os
    raw = (os.environ.get(env_key) or "").strip()
    return [o.strip() for o in raw.split(",") if o.strip()]


def _install_security(app, *, allowed_origins: list[str]) -> None:
    """Security headers + CSRF cho cookie-auth mutation (Origin allow-list check).

    Cookie SameSite=lax đã chặn cross-site form POST; thêm Origin allow-list cho mọi
    method đổi trạng thái = double-guard chuẩn cho cookie-authenticated API.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    _MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
    _CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'")

    class _Sec(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.method in _MUTATING:
                origin = request.headers.get("origin")
                # Cho phép same-origin (không Origin header ở một số client) chỉ khi không cross-site.
                if origin is not None and origin not in allowed_origins:
                    return JSONResponse({"detail": "cross-origin mutation blocked"},
                                        status_code=403)
            resp = await call_next(request)
            resp.headers["Content-Security-Policy"] = _CSP
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return resp

    app.add_middleware(_Sec)


def _sid(request: Request, cookie_name: str,
         authorization: str | None = None) -> str | None:
    """sid từ cookie HttpOnly host-scoped (production) hoặc Bearer (API/test)."""
    c = request.cookies.get(cookie_name)
    if c:
        return c
    auth = authorization or request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    return None


def _install_assets(app) -> None:
    """Phục vụ CSS/JS ngoài same-origin → nạp hợp lệ dưới CSP `default-src 'self'`."""
    @app.get("/assets/shell.css")
    async def _css() -> Response:
        return Response(assets.SHELL_CSS, media_type="text/css",
                        headers={"Cache-Control": "public, max-age=300"})

    @app.get("/assets/app.js")
    async def _js() -> Response:
        return Response(assets.SHELL_JS, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=300"})


# ── PROVIDER APP ─────────────────────────────────────────────────────────────
def create_provider_app(redis, *, oidc_http=None) -> FastAPI:
    app = FastAPI(title="AOIP Provider Operations", version="1.0")
    trace = RuntimeTrace(redis)
    _install_security(app, allowed_origins=_origins("AOIP_PROVIDER_ORIGINS"))
    _install_assets(app)
    _wire_oidc(app, redis, kind=KIND_PROVIDER, cfg_prefix="AOIP_OIDC_PROVIDER_",
               cookie_name=PROVIDER_COOKIE, http_json=oidc_http)

    async def provider(request: Request) -> Principal:
        now = time.time()
        s = await identity.load_session(redis, _sid(request, PROVIDER_COOKIE), now)
        if s is None:
            raise HTTPException(401, "unauthenticated")
        p = s.principal
        if p.kind != KIND_PROVIDER or not p.can(P_VIEW):
            await identity.audit(redis, event="DENIED", subject=p.subject,
                                 detail="provider namespace, insufficient", ts=now)
            raise HTTPException(403, "not a provider viewer")
        return p

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return assets.shell_html("AOIP · Provider Operations", "provider",
                                 "/api/provider/v1", "Provider Operations")

    @app.get("/api/provider/v1/me")
    async def me(p: Principal = Depends(provider)) -> dict:
        return {"subject": p.subject, "kind": p.kind, "roles": list(p.roles),
                "permissions": sorted(p.permissions)}

    @app.get("/api/provider/v1/overview")
    async def overview(request: Request, p: Principal = Depends(provider)) -> dict:
        # Control-tower số THẬT: Trace Spine + agent registry + PG tenant + liveness.
        # Metric chưa có nguồn → {available:false, reason} (không bịa số). RBAC: P_VIEW đủ (read-only).
        pool = getattr(request.app.state, "pool", None)
        return await build_provider_overview(redis, pool, trace, now=time.time())

    @app.get("/api/provider/v1/agents")
    async def agents(p: Principal = Depends(provider)) -> dict:
        # Remote-agent fleet projection từ Redis registry/checks/command ready-set.
        # Không tạo nguồn sự thật thứ hai; chỉ chuẩn hoá thành bảng operator đọc được.
        return await build_provider_agents(redis, now=time.time())

    @app.get("/api/provider/v1/understanding")
    async def understanding(p: Principal = Depends(provider)) -> dict:
        # System Twin + Competency + Unknowns projection từ AOIP runtime store.
        return await build_provider_understanding(redis, now=time.time())

    @app.get("/api/provider/v1/missions")
    async def missions(p: Principal = Depends(provider)) -> dict:
        return {"missions": await MissionStore(redis).list_all(limit=500)}

    @app.get("/api/provider/v1/human-inbox")
    async def human_inbox(p: Principal = Depends(provider)) -> dict:
        # Unknown -> Question projection. May open bounded structured questions
        # from existing Unknowns so the operator can answer them in-product.
        return await build_provider_human_inbox(redis, now=time.time())

    @app.post("/api/provider/v1/questions/{tenant}/{question_id}/answer")
    async def answer_question(tenant: str, question_id: str, body: AnswerQuestionBody,
                              p: Principal = Depends(provider)) -> dict:
        answer = await submit_answer(
            redis, tenant, question_id,
            answered_by=body.answered_by or p.subject,
            value=body.value,
            source_channel="provider_portal",
            confidence=body.confidence,
            now=time.time(),
        )
        if answer is None:
            raise HTTPException(404, "question not found or not pending")
        return {"tenant_id": tenant, "question_id": question_id, "answer": answer}

    @app.get("/api/provider/v1/settings")
    async def settings(request: Request, p: Principal = Depends(provider)) -> dict:
        # Enrollment/credential admin — IT-3 store surfaced in-product instead of curl.
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        return await build_provider_settings(pool)

    @app.post("/api/provider/v1/settings/enroll-tokens")
    async def create_enroll_token_route(
        request: Request, body: EnrollTokenBody, p: Principal = Depends(provider),
    ) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to issue enroll tokens")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        try:
            return await issue_enroll_token(
                pool, tenant_id=body.tenant_id, actor=p.subject, label=body.label,
                ttl_seconds=body.ttl_seconds, environment_id=body.environment_id,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/provider/v1/settings/agent-credentials/{tenant_id}/{agent_id}")
    async def revoke_agent_credential_route(
        request: Request, tenant_id: str, agent_id: str, p: Principal = Depends(provider),
    ) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to revoke credentials")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        redis_state = getattr(request.app.state, "redis", None) or redis
        revoked = await revoke_agent_credential(
            pool, redis_state, tenant_id=tenant_id, agent_id=agent_id, actor=p.subject,
        )
        return {"status": "ok", "revoked": revoked}

    @app.get("/api/provider/v1/audit")
    async def audit_chain(request: Request, p: Principal = Depends(provider)) -> dict:
        # CRAT hash-chain projection — sensitive audit evidence, gated same as
        # raw-evidence viewing (not every provider viewer role).
        if not p.can(P_RAW_EVIDENCE):
            raise HTTPException(403, "insufficient permission to view audit chain")
        tenant_id = request.query_params.get("tenant_id")
        return await build_provider_audit(redis, tenant_id=tenant_id)

    @app.get("/api/provider/v1/tenants")
    async def tenants(request: Request, p: Principal = Depends(provider)) -> dict:
        # PG tenant lifecycle is canonical when the admin store is configured.
        # Redis trace projection remains the lab-compatible fallback.
        pool = getattr(request.app.state, "pool", None)
        if pool is not None:
            from services.admin_config.repo import AdminConfigRepo
            return {"viewer": p.subject, "tenants": await AdminConfigRepo(pool).list_tenants()}
        out = []
        for k in sorted(await redis.keys("trace:index:*")):
            t = k.split("trace:index:", 1)[1]
            out.append({"tenant": t, "incidents": len(await trace.list_timelines(t))})
        return {"viewer": p.subject, "tenants": out}

    @app.post("/api/provider/v1/tenants")
    async def create_tenant(request: Request, body: CreateTenantBody,
                            p: Principal = Depends(provider)) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to create tenants")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        try:
            result = await AdminConfigRepo(pool).create_tenant(
                tenant_id=body.tenant_id.strip(), display_name=body.display_name.strip(),
                actor=p.subject,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "ok", **result}

    @app.get("/api/provider/v1/tenants/{tenant_id}/environments")
    async def environments(request: Request, tenant_id: str,
                           p: Principal = Depends(provider)) -> dict:
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        return {"tenant_id": tenant_id,
                "environments": await AdminConfigRepo(pool).list_environments(tenant_id)}

    @app.post("/api/provider/v1/tenants/{tenant_id}/environments")
    async def create_environment(request: Request, tenant_id: str,
                                 body: CreateEnvironmentBody,
                                 p: Principal = Depends(provider)) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to create environments")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        try:
            result = await AdminConfigRepo(pool).create_environment(
                tenant_id=tenant_id, environment_id=body.environment_id,
                display_name=body.display_name, environment_type=body.environment_type,
                actor=p.subject,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "ok", **result}

    @app.post("/api/provider/v1/tenants/{tenant_id}/environments/{environment_id}/status")
    async def set_environment_status(request: Request, tenant_id: str,
                                     environment_id: str, body: EnvironmentStatusBody,
                                     p: Principal = Depends(provider)) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to change environments")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        try:
            result = await AdminConfigRepo(pool).set_environment_status(
                tenant_id=tenant_id, environment_id=environment_id,
                status=body.status, actor=p.subject,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "ok", **result}

    @app.get("/api/provider/v1/tenants/{tenant_id}/autonomy")
    async def autonomy(request: Request, tenant_id: str,
                       p: Principal = Depends(provider)) -> dict:
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        repo = AdminConfigRepo(pool)
        current = await repo.get_tier(tenant_id) or "shadow"
        raw = await redis.get(f"omni:tier:readiness:{tenant_id}")
        readiness = None
        if raw:
            import json
            readiness = json.loads(raw)
        return {"tenant_id": tenant_id, "tier": current, "readiness": readiness}

    @app.get("/api/provider/v1/tenants/{tenant_id}/plan")
    async def tenant_plan(request: Request, tenant_id: str,
                          p: Principal = Depends(provider)) -> dict:
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        plan = await AdminConfigRepo(pool).get_tenant_plan(tenant_id)
        if plan is None:
            raise HTTPException(404, "tenant plan unavailable")
        return plan

    @app.post("/api/provider/v1/tenants/{tenant_id}/plan")
    async def set_tenant_plan(request: Request, tenant_id: str, body: TenantPlanBody,
                              p: Principal = Depends(provider)) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to change tenant plan")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        try:
            result = await AdminConfigRepo(pool).set_tenant_plan(
                tenant_id=tenant_id, plan_code=body.plan_code, agent_limit=body.agent_limit,
                autonomy_ceiling=body.autonomy_ceiling, retention_days=body.retention_days,
                support_tier=body.support_tier, enabled=body.enabled, actor=p.subject,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "ok", **result}

    @app.post("/api/provider/v1/tenants/{tenant_id}/autonomy")
    async def set_autonomy(request: Request, tenant_id: str, body: AutonomyTierBody,
                           p: Principal = Depends(provider)) -> dict:
        if not p.can(P_CHANGE_POLICY):
            raise HTTPException(403, "insufficient permission to change autonomy")
        if body.tier not in {"shadow", "assist", "auto"}:
            raise HTTPException(400, "tier không hợp lệ")
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin PG store not configured")
        from services.admin_config.repo import AdminConfigRepo
        repo = AdminConfigRepo(pool)
        current = await repo.get_tier(tenant_id) or "shadow"
        rank = {"shadow": 0, "assist": 1, "auto": 2}
        if rank[body.tier] > rank[current] and not body.confirm:
            raise HTTPException(409, f"Nâng tier {current}→{body.tier} cần confirm=true")
        raw = await redis.get(f"omni:tier:readiness:{tenant_id}")
        readiness = {}
        if raw:
            import json
            readiness = json.loads(raw)
        try:
            result = await repo.set_tier(
                tenant_id=tenant_id, tier=body.tier, actor=p.subject,
                readiness=readiness, forced=body.forced,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "ok", "from": current, "to": body.tier,
                "promotion": rank[body.tier] > rank[current], **result}

    @app.get("/api/provider/v1/operations")
    async def operations(p: Principal = Depends(provider)) -> dict:
        items = []
        for k in sorted(await redis.keys("trace:index:*")):
            t = k.split("trace:index:", 1)[1]
            for cid in await trace.list_timelines(t):
                ev = await trace.timeline(t, cid)
                v = provider_incident(ev, include_raw=False)
                if v.get("reconcile_required") or not v.get("reported"):
                    items.append({"tenant": t, "correlation_id": cid,
                                  "phase": v["execution_phase"],
                                  "reconcile_required": v["reconcile_required"]})
        return {"operations": items}

    @app.get("/api/provider/v1/incidents")
    async def incidents(p: Principal = Depends(provider)) -> dict:
        return await list_provider_lab_incidents(redis, trace)

    @app.post("/api/provider/v1/lab/incidents")
    async def create_lab_incident_route(body: CreateLabIncidentBody,
                                        p: Principal = Depends(provider)) -> dict:
        try:
            return await create_lab_incident(
                redis,
                tenant_id=body.tenant_id,
                agent_id=body.agent_id,
                host=body.host,
                service=body.service,
                unit=body.unit,
                requested_by=p.subject,
                now=time.time(),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/provider/v1/incident/{tenant}/{correlation_id}")
    async def incident(request: Request, tenant: str, correlation_id: str, raw: bool = False,
                       p: Principal = Depends(provider)) -> dict:
        ev = await trace.timeline(tenant, correlation_id)
        if not ev:
            raise HTTPException(404, "no such incident")
        include_raw = False
        if raw:
            # Gate 2 lớp: permission P_RAW_EVIDENCE + support-access grant còn hiệu lực (PG).
            if not p.can(P_RAW_EVIDENCE):
                await identity.audit(redis, event="DENIED", subject=p.subject, tenant=tenant,
                                     detail="raw evidence without permission", ts=time.time())
                raise HTTPException(403, "raw evidence requires explicit audited permission")
            pool = getattr(request.app.state, "pool", None)
            if pool is not None and not await identity_store.has_active_support_grant(
                    pool, subject=p.subject, tenant=tenant):
                await identity.audit(redis, event="DENIED", subject=p.subject, tenant=tenant,
                                     detail="raw evidence without active support grant",
                                     ts=time.time())
                raise HTTPException(403, "raw evidence requires an active audited support grant")
            include_raw = True
        if include_raw:  # truy cập raw tenant evidence = phiên support, PHẢI audit
            await identity.audit(redis, event="SUPPORT_ACCESS", subject=p.subject, tenant=tenant,
                                 detail=f"raw_evidence:{correlation_id}", ts=time.time())
            pool = getattr(request.app.state, "pool", None)
            if pool is not None:
                await identity_store.persist_audit(
                    pool, event="SUPPORT_ACCESS", subject=p.subject, tenant=tenant,
                    detail=f"raw_evidence:{correlation_id}")
        return {"view": "provider", "incident": provider_incident(ev, include_raw=include_raw)}

    @app.get("/api/provider/v1/support-access/{tenant}")
    async def support_access(tenant: str, p: Principal = Depends(provider)) -> dict:
        events = await identity.read_audit(redis, limit=500)
        sessions = [{"subject": e["subject"], "detail": e["detail"], "ts": e["ts"]}
                    for e in events if e["event"] == "SUPPORT_ACCESS" and e["tenant"] == tenant]
        return {"tenant": tenant, "sessions": sessions}

    @app.post("/api/provider/v1/logout")
    async def logout(request: Request) -> Response:
        await identity.revoke_session(redis, _sid(request, PROVIDER_COOKIE), now=time.time())
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(PROVIDER_COOKIE, path=os.environ.get("AOIP_SESSION_COOKIE_PATH", "/"))
        return resp

    return app


# ── TENANT APP ───────────────────────────────────────────────────────────────
def create_tenant_app(redis, *, oidc_http=None) -> FastAPI:
    app = FastAPI(title="AOIP Tenant Operations", version="1.0")
    trace = RuntimeTrace(redis)
    _install_security(app, allowed_origins=_origins("AOIP_TENANT_ORIGINS"))
    _install_assets(app)
    _wire_oidc(app, redis, kind=KIND_TENANT, cfg_prefix="AOIP_OIDC_TENANT_",
               cookie_name=TENANT_COOKIE, http_json=oidc_http)

    async def tenant_principal(request: Request) -> Principal:
        now = time.time()
        s = await identity.load_session(redis, _sid(request, TENANT_COOKIE), now)
        if s is None:
            raise HTTPException(401, "unauthenticated")
        p = s.principal
        if p.kind != KIND_TENANT or not p.tenant or not p.can(P_VIEW):
            await identity.audit(redis, event="DENIED", subject=p.subject,
                                 detail="tenant namespace, insufficient", ts=now)
            raise HTTPException(403, "not a tenant viewer")
        return p

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return assets.shell_html("AOIP · Your Operations", "tenant",
                                 "/api/tenant/v1", "Tenant Operations")

    @app.get("/api/tenant/v1/me")
    async def me(p: Principal = Depends(tenant_principal)) -> dict:
        # active org = tenant của session; memberships để đổi org (chỉ nơi được phép).
        return {"subject": p.subject, "kind": p.kind, "active_tenant": p.tenant,
                "roles": list(p.roles), "permissions": sorted(p.permissions),
                "memberships": await identity.list_memberships(redis, p.subject)}

    @app.get("/api/tenant/v1/incidents")
    async def incidents(p: Principal = Depends(tenant_principal)) -> dict:
        # tenant TỪ principal — KHÔNG nhận tenant_id từ client.
        return {"tenant": p.tenant, "incidents": await trace.list_timelines(p.tenant)}

    @app.get("/api/tenant/v1/incident/{correlation_id}")
    async def incident(correlation_id: str, p: Principal = Depends(tenant_principal)) -> dict:
        ev = await trace.timeline(p.tenant, correlation_id)
        if not ev:
            raise HTTPException(404, "no such incident for your tenant")
        # Chốt chặn cross-tenant: mọi event phải thuộc đúng tenant của principal.
        if any(e["tenant_id"] != p.tenant for e in ev):
            await identity.audit(redis, event="DENIED", subject=p.subject, tenant=p.tenant,
                                 detail=f"cross-tenant read {correlation_id}", ts=time.time())
            raise HTTPException(403, "cross-tenant access denied")
        include_raw = p.can(P_RAW_EVIDENCE)
        return {"view": "tenant", "incident": tenant_incident(ev, include_raw=include_raw)}

    @app.get("/api/tenant/v1/approvals")
    async def approvals(p: Principal = Depends(tenant_principal)) -> dict:
        return {"tenant": p.tenant, "pending": await trace.pending_approvals(p.tenant)}

    # ── Reports (G4) — worker sinh, portal chỉ đọc ────────────────────────────
    # Dữ liệu do `workers.capacity_loops.capacity_report_loop` publish vào Redis.
    # Gateway đã có `/reports/*` nhưng tenant portal KHÔNG đi qua gateway (nó gọi
    # thẳng app này), nên phải phơi ở đây. Tenant lấy TỪ PRINCIPAL — không có
    # tham số tenant_id nào để client can thiệp, khác hẳn bề mặt gateway vốn phải
    # dùng resolve_scope vì nhận tenant_id trên query string.
    @app.get("/api/tenant/v1/reports/sre")
    async def tenant_sre_report(p: Principal = Depends(tenant_principal)) -> dict:
        raw = await redis.get(f"omni:report:sre:{p.tenant}")
        if not raw:
            raise HTTPException(404, "chưa có báo cáo cho tenant này — worker chưa sinh lần nào")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return {"tenant": p.tenant, "report": raw}

    @app.get("/api/tenant/v1/reports/capacity")
    async def tenant_capacity_advice(p: Principal = Depends(tenant_principal)) -> dict:
        """Đề xuất dung lượng. LUÔN là văn bản — không kèm tool/args chạy được."""
        raw = await redis.get(f"omni:capacity:advice:{p.tenant}")
        if not raw:
            return {"tenant": p.tenant, "advice": []}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            import json as _json

            advice = _json.loads(raw)
        except (TypeError, ValueError):
            advice = []
        return {"tenant": p.tenant, "advice": advice}

    @app.get("/api/tenant/v1/hitl/pending")
    async def tenant_hitl_pending(p: Principal = Depends(tenant_principal)) -> dict:
        """Hàng đợi HITL từ PostgreSQL (`omni_admin.hitl_decision`).

        Khác `/approvals`: `/approvals` đọc Redis trace (read-model của runtime), còn
        đây là sổ HITL bền vững có CRAT. Hai nguồn khác nhau, cố ý phơi cả hai.

        Read-only. Quyền QUYẾT ĐỊNH cố ý KHÔNG có ở portal tenant: nó nằm ở
        `/autonomy/hitl/{id}/decide` trên gateway, nơi có ledger CRAT + outbox +
        publish Kafka. Nhân bản đường ghi đó ở đây sẽ tạo con đường thứ hai vào
        cùng một ledger — chính xác là thứ không nên có với dữ liệu chịu kiểm toán.
        """
        pool = getattr(app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin store chưa sẵn sàng")
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pending_id, tool_name, risk_class, tier_at_time, channel, "
                "actor, created_at FROM omni_admin.hitl_decision "
                "WHERE tenant_id = $1 AND decision = 'PENDING' "
                "ORDER BY created_at DESC LIMIT 100",
                p.tenant,
            )
        return {
            "tenant": p.tenant,
            "pending": [
                {
                    "pending_id": r["pending_id"],
                    "tool_name": r["tool_name"],
                    "risk_class": r["risk_class"],
                    "tier_at_time": r["tier_at_time"],
                    "channel": r["channel"],
                    "actor": r["actor"],
                    # isoformat tại đây: JSONResponse không encode datetime và đã
                    # từng làm endpoint /reports/playbooks của gateway trả 500.
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ],
        }

    # ── Sổ ca: hồ sơ năng lực + đơn xin quyền ────────────────────────────────
    # Gateway đã có `/competency/*`, nhưng tenant portal KHÔNG đi qua gateway
    # (xem ghi chú ở /reports/* phía trên). Ba route dưới đây là CÙNG một logic
    # (`services.case_ledger`), chỉ khác chỗ lấy tenant: gateway nhận `tenant_id`
    # trên query string nên phải `resolve_scope`, còn ở đây tenant lấy từ
    # principal và client không có tham số nào để can thiệp.
    def _case_ledger_stores():
        pool = getattr(app.state, "pool", None)
        if pool is None:
            raise HTTPException(503, "admin store chưa sẵn sàng")
        from services.case_ledger.store import CaseLedgerStore
        from services.case_ledger.store_scope import ScopeStore

        return CaseLedgerStore(pool), ScopeStore(pool)

    def _jsonable_rows(value):
        """asyncpg trả TIMESTAMPTZ là ``datetime`` — JSONResponse không encode được."""
        from datetime import date, datetime
        from decimal import Decimal

        if isinstance(value, dict):
            return {k: _jsonable_rows(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable_rows(v) for v in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    @app.get("/api/tenant/v1/competency/patterns")
    async def tenant_competency_patterns(p: Principal = Depends(tenant_principal)) -> dict:
        """Hồ sơ năng lực từng loại việc. Trả CẢ pattern chưa đủ điều kiện —
        ``blockers`` là phần đáng giá nhất với admin khách."""
        ledger, scope = _case_ledger_stores()
        from services.case_ledger.advocacy import ScopeAdvocate

        advocate = ScopeAdvocate(ledger, scope)
        reports = await advocate.build_reports(tenant_id=p.tenant)
        grants = {
            str(g["pattern_key"]): g for g in await scope.list_grants(tenant_id=p.tenant)
        }
        patterns = []
        for rep in reports:
            grant = grants.get(rep.pattern_key) or {}
            patterns.append(
                {
                    **rep.as_dict(),
                    "granted_scope": grant.get("granted_scope", "SUGGEST_ONLY"),
                    "frozen": bool(grant.get("frozen", False)),
                    "frozen_reason": grant.get("frozen_reason"),
                }
            )
        return {"tenant_id": p.tenant, "patterns": patterns}

    @app.get("/api/tenant/v1/competency/scope-requests")
    async def tenant_scope_requests(p: Principal = Depends(tenant_principal)) -> dict:
        """Đơn Omni đã nộp, kèm ``evidence`` đóng băng lúc nộp."""
        _, scope = _case_ledger_stores()
        rows = await scope.list_requests(tenant_id=p.tenant, limit=200)
        return {"tenant_id": p.tenant, "requests": _jsonable_rows(rows)}

    @app.post("/api/tenant/v1/competency/scope-requests/{request_id}/decide")
    async def tenant_decide_scope_request(
        request_id: int, body: ScopeDecisionBody,
        p: Principal = Depends(tenant_principal),
    ) -> dict:
        """Duyệt/từ chối. Trao quyền thực thi cho một pattern nên phải là
        ``P_CHANGE_POLICY``, không phải quyền xem."""
        if not p.can(P_CHANGE_POLICY):
            await identity.audit(redis, event="DENIED", subject=p.subject, tenant=p.tenant,
                                 detail=f"scope decide {request_id}", ts=time.time())
            raise HTTPException(403, "không có quyền thay đổi chính sách")
        decision = body.decision.upper()
        if decision not in ("APPROVED", "REJECTED"):
            raise HTTPException(400, f"decision không hợp lệ: {body.decision}")
        _, scope = _case_ledger_stores()
        from services.case_ledger.advocacy import approve_request, reject_request

        try:
            if decision == "APPROVED":
                row = await approve_request(
                    scope, request_id=request_id, tenant_id=p.tenant,
                    actor=p.subject, note=body.note,
                )
            else:
                row = await reject_request(
                    scope, request_id=request_id, tenant_id=p.tenant,
                    actor=p.subject, note=body.note, cooldown_days=body.cooldown_days,
                )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if row is None:
            raise HTTPException(404, "đơn không tồn tại hoặc đã được phán quyết")
        return {"status": "ok", **_jsonable_rows(row)}

    @app.get("/api/tenant/v1/agents")
    async def agents(p: Principal = Depends(tenant_principal)) -> dict:
        # Filter at the projection boundary, before returning any cross-tenant
        # registry record to the tenant portal.
        return await build_provider_agents(redis, now=time.time(), tenant_id=p.tenant)

    @app.get("/api/tenant/v1/understanding")
    async def understanding(p: Principal = Depends(tenant_principal)) -> dict:
        projection = await build_provider_understanding(redis, now=time.time(), tenant_id=p.tenant)
        # A tenant principal can only receive its own slice. An empty projection
        # is a valid no-discovery state, not permission to enumerate other tenants.
        return projection

    @app.get("/api/tenant/v1/missions")
    async def tenant_missions(p: Principal = Depends(tenant_principal)) -> dict:
        return {"tenant": p.tenant, "missions": await MissionStore(redis).list(p.tenant, limit=200)}

    @app.post("/api/tenant/v1/logout")
    async def logout(request: Request) -> Response:
        await identity.revoke_session(redis, _sid(request, TENANT_COOKIE), now=time.time())
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(TENANT_COOKIE, path=os.environ.get("AOIP_SESSION_COOKIE_PATH", "/"))
        return resp

    return app
