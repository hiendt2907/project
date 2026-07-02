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
from aoip.console.authz import KIND_PROVIDER, KIND_TENANT, P_RAW_EVIDENCE, P_VIEW, Principal
from aoip.console.human_inbox import build_provider_human_inbox
from aoip.console.lab_incidents import create_lab_incident, list_provider_lab_incidents
from aoip.console.overview import build_provider_overview
from aoip.console.projections import provider_incident, tenant_incident
from aoip.console.understanding import build_provider_understanding
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


class CreateLabIncidentBody(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=128)
    agent_id: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=256)
    service: str = Field(..., min_length=1, max_length=128)
    unit: str = Field(..., min_length=1, max_length=128)


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

    @app.get("/api/provider/v1/tenants")
    async def tenants(p: Principal = Depends(provider)) -> dict:
        out = []
        for k in sorted(await redis.keys("trace:index:*")):
            t = k.split("trace:index:", 1)[1]
            out.append({"tenant": t, "incidents": len(await trace.list_timelines(t))})
        return {"viewer": p.subject, "tenants": out}

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

    @app.post("/api/tenant/v1/logout")
    async def logout(request: Request) -> Response:
        await identity.revoke_session(redis, _sid(request, TENANT_COOKIE), now=time.time())
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(TENANT_COOKIE, path=os.environ.get("AOIP_SESSION_COOKIE_PATH", "/"))
        return resp

    return app
