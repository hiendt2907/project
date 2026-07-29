"""Báo cáo SRE / đề xuất capacity / playbook đã tốt nghiệp — read-only.

Nguồn dữ liệu do worker sinh ra, gateway chỉ đọc:
- ``omni:report:sre:{tenant}``     ← `workers.capacity_loops.capacity_report_loop`
- ``omni:capacity:advice:{tenant}``← cùng loop trên
- ``omni_admin.playbook_graduation`` ← `services.learning_promoter.advisory_promoter`

CÁCH LY TENANT: mọi endpoint đi qua ``resolve_scope`` — tenant thường KHÔNG thể đọc dữ
liệu tenant khác dù truyền ``tenant_id`` khác trên query string (override chỉ có hiệu lực
với admin). Đây là ranh giới quan trọng nhất của router này vì portal khách hàng gọi thẳng
vào đây.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from gateway.tenant_context import get_tenant_ctx, resolve_scope

router = APIRouter(prefix="/reports", tags=["reports"])

_REPORT_KEY = "omni:report:sre:{tenant}"
_ADVICE_KEY = "omni:capacity:advice:{tenant}"
_DEFAULT_TENANT = "default"


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


def _effective_tenant(request: Request, override: str | None) -> str:
    """Tenant thực sự được phép đọc. Non-admin luôn bị ép về tenant của chính mình."""
    ctx = get_tenant_ctx(request)
    scoped = resolve_scope(ctx, override)
    # scoped=None nghĩa là admin/lab không chỉ định tenant nào → mặc định tenant lab.
    return scoped or (override if ctx is None else None) or _DEFAULT_TENANT


@router.get("/sre")
async def get_sre_report(
    request: Request,
    tenant_id: str | None = Query(default=None, pattern=r"^[a-zA-Z0-9_-]+$"),
) -> JSONResponse:
    """Báo cáo vận hành dạng markdown cho tenant hiệu lực."""
    tenant = _effective_tenant(request, tenant_id)
    redis = _get_redis(request)
    report = await redis.get(_REPORT_KEY.format(tenant=tenant))
    if not report:
        raise HTTPException(
            status_code=404,
            detail=f"Chưa có báo cáo cho tenant '{tenant}' — worker chưa sinh lần nào.",
        )
    if isinstance(report, bytes):
        report = report.decode("utf-8", errors="replace")
    return JSONResponse(content={"tenant_id": tenant, "report": report})


@router.get("/capacity")
async def get_capacity_advice(
    request: Request,
    tenant_id: str | None = Query(default=None, pattern=r"^[a-zA-Z0-9_-]+$"),
) -> JSONResponse:
    """Đề xuất dung lượng. Luôn là văn bản đề xuất — không kèm tool/args chạy được."""
    tenant = _effective_tenant(request, tenant_id)
    redis = _get_redis(request)
    raw = await redis.get(_ADVICE_KEY.format(tenant=tenant))
    if not raw:
        return JSONResponse(content={"tenant_id": tenant, "advice": []})
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        advice = json.loads(raw)
    except (TypeError, ValueError):
        advice = []
    return JSONResponse(content={"tenant_id": tenant, "advice": advice})


@router.get("/playbooks")
async def get_graduated_playbooks(
    request: Request,
    tenant_id: str | None = Query(default=None, pattern=r"^[a-zA-Z0-9_-]+$"),
    state: str | None = Query(default=None, pattern=r"^[A-Z]+$"),
) -> JSONResponse:
    """Playbook đã tích luỹ qua vòng học (G1), kèm số lần đúng/sai."""
    tenant = _effective_tenant(request, tenant_id)
    repo = getattr(request.app.state, "admin_repo", None)
    if repo is None or not hasattr(repo, "list_playbook_graduations"):
        raise HTTPException(status_code=503, detail="Admin store not available")
    rows = await repo.list_playbook_graduations(tenant, state=state)
    # Row Postgres chứa cột timestamp — JSONResponse không tự serialize datetime,
    # phải đi qua jsonable_encoder (bug 500 phát hiện lúc smoke test trên cluster;
    # test unit không bắt được vì repo bị mock trả dict thuần).
    return JSONResponse(
        content=jsonable_encoder({"tenant_id": tenant, "playbooks": [dict(r) for r in rows]})
    )
