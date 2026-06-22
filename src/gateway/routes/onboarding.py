"""Onboarding routes — read-only Mermaid diagram + manual handover-doc upload.

step-3 của agent/plans/PLAN_onboarding_ops_agent.md. Uses pkg.onboarding.discovery_doc
(dependency-light, shared with the onboarding worker) — gateway never imports workers/.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.tenant_context import get_tenant_ctx, resolve_scope
from pkg.onboarding import discovery_doc as dd

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

_TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


def _get_admin_repo(request: Request) -> Any:
    repo = getattr(request.app.state, "admin_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail="Admin config store offline (OMNI_ADMIN_PG_DSN chưa cấu hình)",
        )
    return repo


def _effective_tenant_id(request: Request, tenant_id: str | None) -> str:
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, tenant_id)
    return scope or tenant_id or "default"


@router.get("/diagram")
async def get_diagram(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Latest versioned Mermaid diagram text (raw — never rendered to image)."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    result = await dd.get_latest_diagram(redis, scope)
    if result is None:
        return JSONResponse(content={"tenant_id": scope, "version": None, "mermaid": None})
    version, text = result
    return JSONResponse(content={"tenant_id": scope, "version": version, "mermaid": text})


@router.get("/diagram/history")
async def get_diagram_history(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
    from_version: int = Query(default=1, ge=1),
    to_version: int = Query(default=20, ge=1, le=200),
) -> JSONResponse:
    """Past diagram versions (diffable) — bounded range to avoid unbounded scans."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    versions: list[dict[str, Any]] = []
    for v in range(from_version, to_version + 1):
        text = await dd.get_diagram_version(redis, scope, v)
        if text is not None:
            versions.append({"version": v, "mermaid": text})
    return JSONResponse(content={"tenant_id": scope, "versions": versions})


@router.get("/doc")
async def get_doc(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Accumulated raw discovery facts per probe — for the onboarding UI."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    doc = await dd.get_accumulated_doc(redis, scope)
    return JSONResponse(content={"tenant_id": scope, "doc": doc})


@router.get("/readiness")
async def get_readiness(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Readiness checklist (Postgres source-of-truth via AdminConfigRepo)."""
    repo = _get_admin_repo(request)
    scope = _effective_tenant_id(request, tenant_id)
    readiness = await repo.get_tenant_readiness(scope)
    return JSONResponse(content={"tenant_id": scope, "readiness": readiness})


class HandoverDocUpload(BaseModel):
    filename: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=8000)
    tenant_id: str | None = Field(default=None, pattern=_TENANT_ID_PATTERN)


@router.post("/handover-doc")
async def upload_handover_doc(request: Request, body: HandoverDocUpload) -> JSONResponse:
    """A8: manually-uploaded handover doc — feeds the same A3 accumulation pipeline
    as a doc_snapshot probe, without going through the remote agent/Kafka.

    Data residency: content is hashed in dd.accumulate_probe_fact's sanitization
    step and never persisted on the Omni side — only path/hash/length."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, body.tenant_id)
    discovery_data = {"documents": [{"path": body.filename, "content": body.content}]}
    try:
        await dd.accumulate_probe_fact(redis, scope, "doc_snapshot", discovery_data)
        version = await dd.regenerate_diagrams(redis, scope)
    except Exception as exc:
        logger.error("onboarding.upload_handover_doc error tenant=%s err=%s", scope, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    repo = getattr(request.app.state, "admin_repo", None)
    readiness: dict[str, Any] | None = None
    if repo is not None:
        try:
            fields = await dd.compute_readiness(redis, repo, scope)
            readiness = await repo.set_tenant_readiness(tenant_id=scope, **fields)
        except Exception as exc:  # noqa: BLE001 — readiness recompute best-effort here
            logger.warning("onboarding.upload_handover_doc readiness recompute failed tenant=%s err=%s", scope, exc)

    return JSONResponse(content={"status": "ok", "tenant_id": scope, "diagram_version": version, "readiness": readiness})
