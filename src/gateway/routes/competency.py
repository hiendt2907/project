"""Hồ sơ năng lực + đơn xin quyền của Omni — mặt đọc cho portal, mặt duyệt cho admin.

Thiết kế: `plans/case-ledger-design-2026-07-30.md`.

Route này chỉ phục vụ **số liệu tái dựng được**: mọi con số trả ra đều tính từ
``omni_admin.case_ledger`` bằng ``build_competency_report`` (cận dưới Wilson), và
``evidence`` của mỗi đơn là bản đóng băng lúc nộp. Không có trường nào do LLM sinh
ra — khách phải đối chiếu được với sổ ca, không phải đọc một bản tóm tắt thuyết phục.

Bất biến: gateway KHÔNG import ``workers/``. Toàn bộ logic dùng chung nằm ở
``services.case_ledger``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/competency", tags=["competency"])

_DEFAULT_TENANT = "default"


class DecideScopeRequest(BaseModel):
    decision: str = Field(..., description="APPROVED|REJECTED")
    actor: str = Field(default="admin_ui")
    note: str = Field(default="")
    # Cố ý KHÔNG có trường tenant_id: id đơn đã đủ để định vị, và mọi trường
    # tenant do client gửi đều là một bề mặt tấn công phải kiểm tra lại.
    cooldown_days: int = Field(default=14, ge=0, le=365)


def _competency_tenant(request: Request, requested: str | None) -> str:
    """Tenant thực sự được phép đọc/ghi — KHÔNG lấy thẳng từ client.

    Đây chính là lỗ hổng vừa vá ở ``/autonomy/hitl/*``: SQL vốn đã lọc theo
    ``tenant_id``, nhưng client tự quyết định giá trị đem đi lọc, nên tenant A đọc
    được — và phê duyệt được — dữ liệu của tenant B. Ở đây hậu quả tương đương:
    hồ sơ năng lực là bằng chứng kinh doanh, còn ``/decide`` thì **cấp quyền thực
    thi** cho một pattern.

    ``resolve_scope`` trả None cho lab (ctx=None) và cho admin không chỉ định
    tenant — khi đó giữ giá trị client yêu cầu để không phá hành vi admin/lab.
    """
    from gateway.tenant_context import get_tenant_ctx, resolve_scope

    ctx = get_tenant_ctx(request)
    scoped = resolve_scope(ctx, requested)
    if scoped is not None:
        return scoped
    return requested or _DEFAULT_TENANT


def _stores(request: Request) -> tuple[Any, Any]:
    """(CaseLedgerStore, ScopeStore) dựng trên ``app.state.admin_pool``."""
    pool = getattr(request.app.state, "admin_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="admin store not available")
    from services.case_ledger.store import CaseLedgerStore
    from services.case_ledger.store_scope import ScopeStore

    return CaseLedgerStore(pool), ScopeStore(pool)


@router.get("/patterns")
async def list_patterns(
    request: Request,
    tenant_id: str = Query(default=_DEFAULT_TENANT, pattern=r"^[a-zA-Z0-9_.-]+$"),
) -> JSONResponse:
    """Hồ sơ năng lực từng ``pattern_key``, kèm lý do chưa đủ điều kiện xin quyền.

    Trả cả pattern KHÔNG đủ điều kiện — ``blockers`` là phần có giá trị nhất với
    admin khách: nó nói Omni đang thiếu gì, thay vì chỉ khoe chỗ nó mạnh.
    """
    tenant = _competency_tenant(request, tenant_id)
    ledger, scope = _stores(request)
    from services.case_ledger.advocacy import ScopeAdvocate

    advocate = ScopeAdvocate(ledger, scope)
    try:
        reports = await advocate.build_reports(tenant_id=tenant)
        grants = {
            str(g["pattern_key"]): g for g in await scope.list_grants(tenant_id=tenant)
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("competency.list_patterns error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = []
    for rep in reports:
        grant = grants.get(rep.pattern_key) or {}
        payload.append(
            {
                **rep.as_dict(),
                "granted_scope": grant.get("granted_scope", "SUGGEST_ONLY"),
                "frozen": bool(grant.get("frozen", False)),
                "frozen_reason": grant.get("frozen_reason"),
            }
        )
    return JSONResponse(content={"tenant_id": tenant, "patterns": payload})


@router.get("/scope-requests")
async def list_scope_requests(
    request: Request,
    tenant_id: str = Query(default=_DEFAULT_TENANT, pattern=r"^[a-zA-Z0-9_.-]+$"),
    state: str | None = Query(default=None, pattern=r"^[A-Z]+$"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> JSONResponse:
    """Các đơn Omni đã nộp, kèm ``evidence`` đóng băng lúc nộp."""
    tenant = _competency_tenant(request, tenant_id)
    _, scope = _stores(request)
    try:
        rows = await scope.list_requests(tenant_id=tenant, state=state, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("competency.list_scope_requests error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(
        content={"tenant_id": tenant, "requests": _jsonable(rows)}
    )


@router.post("/scope-requests/{request_id}/decide")
async def decide_scope_request(
    request: Request, request_id: int, body: DecideScopeRequest
) -> JSONResponse:
    """Người duyệt/từ chối. APPROVED thì ghi luôn ``scope_grant``.

    ``tenant`` được suy ra từ danh tính, rồi đi vào **mệnh đề WHERE** của câu
    UPDATE. Nhờ vậy một ``request_id`` đoán được cũng không chạm sang tenant khác
    — đơn không thuộc phạm vi thì trả 404, không phải 403, để không xác nhận sự
    tồn tại của đơn thuộc tenant khác.
    """
    decision = body.decision.upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail=f"decision không hợp lệ: {body.decision}")

    tenant = _competency_tenant(request, None)
    _, scope = _stores(request)
    from services.case_ledger.advocacy import approve_request, reject_request

    try:
        if decision == "APPROVED":
            row = await approve_request(
                scope, request_id=request_id, tenant_id=tenant,
                actor=body.actor, note=body.note,
            )
        else:
            row = await reject_request(
                scope, request_id=request_id, tenant_id=tenant,
                actor=body.actor, note=body.note, cooldown_days=body.cooldown_days,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("competency.decide_scope_request error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if row is None:
        raise HTTPException(
            status_code=404, detail="đơn không tồn tại hoặc đã được phán quyết"
        )
    return JSONResponse(content={"status": "ok", **_jsonable(row)})


def _jsonable(value: Any) -> Any:
    """asyncpg trả TIMESTAMPTZ là ``datetime`` — JSONResponse không encode được."""
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
