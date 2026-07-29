"""Compliance export endpoints — CRAT audit chain CSV/JSON export and stats.

Read-only gateway route. No worker imports.
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from gateway.tenant_context import get_tenant_ctx, resolve_scope
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(prefix="/crat", tags=["compliance"])

_DEFAULT_BLOCKS_KEY = "audit_chain:blocks"
_DEFAULT_HEAD_KEY = "audit_chain:head_hash"
_DEFAULT_SEQ_KEY = "audit_chain:seq"

_MAX_EXPORT_DAYS = 90
_DEFAULT_EXPORT_DAYS = 30


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


def _blocks_key(tenant_id: str) -> str:
    if tenant_id == "default":
        return _DEFAULT_BLOCKS_KEY
    return f"audit_chain:{tenant_id}:blocks"


def _head_key(tenant_id: str) -> str:
    if tenant_id == "default":
        return _DEFAULT_HEAD_KEY
    return f"audit_chain:{tenant_id}:head_hash"


def _seq_key(tenant_id: str) -> str:
    if tenant_id == "default":
        return _DEFAULT_SEQ_KEY
    return f"audit_chain:{tenant_id}:seq"


def _cutoff_timestamp(days: int) -> str:
    """Return ISO timestamp for `days` ago (UTC)."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff_dt.isoformat()


def _parse_block(raw: str) -> dict[str, Any] | None:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _block_to_row(block: dict[str, Any]) -> dict[str, str]:
    """Flatten block to CSV-compatible row."""
    return {
        "seq": str(block.get("seq", "")),
        "timestamp": block.get("timestamp_utc", ""),
        "event_type": block.get("event_type", ""),
        "trace_id": block.get("trace_id", ""),
        "tenant_id": block.get("tenant_id", "default"),
        "block_hash": block.get("block_hash", ""),
        "prev_hash": block.get("prev_hash", ""),
        "has_signature": "true" if block.get("signature_hex") else "false",
    }


_CSV_FIELDNAMES = ["seq", "timestamp", "event_type", "trace_id", "tenant_id", "block_hash", "prev_hash", "has_signature"]


async def _fetch_blocks(redis: Any, tenant_id: str, days: int) -> list[dict[str, Any]]:
    """Fetch all blocks for the tenant and filter to last `days` days."""
    key = _blocks_key(tenant_id)
    try:
        raw_list: list[str] = await redis.lrange(key, 0, -1)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis read error: {exc}") from exc

    cutoff = _cutoff_timestamp(days)
    blocks: list[dict[str, Any]] = []
    for raw in raw_list:
        block = _parse_block(raw)
        if block is None:
            continue
        # Filter by timestamp — blocks older than cutoff are excluded
        ts = block.get("timestamp_utc", "")
        if ts and ts < cutoff:
            continue
        blocks.append(block)
    return blocks


def _effective_tenant(request: Request, requested: str) -> str:
    """Tenant thực sự được phép đọc chain CRAT.

    Trước 2026-07-29 hàm này không tồn tại và `tenant_id` từ query được dùng thẳng để
    dựng key `audit_chain:{tenant_id}:blocks` — bất kỳ tenant nào cũng export được chain
    audit của tenant khác. Đây là dữ liệu SOX §404 / PCI-DSS v4.0 nên đó là vi phạm
    compliance, không chỉ là bug. `siem.py` cùng đọc key này và đã làm đúng từ đầu.
    """
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, requested)
    if scope is not None:
        return scope
    # scope=None có 2 nghĩa khác nhau, không được gộp:
    #  - ctx is None  → lab/no-auth: giữ backward-compat, tôn trọng tenant_id được hỏi.
    #  - ctx.is_admin → admin không chỉ định tenant nào → chain global "default".
    return requested if ctx is None else "default"


@router.get("/export", response_model=None)
async def crat_export(
    request: Request,
    tenant_id: str = Query(default="default", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"),
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    days: int = Query(default=_DEFAULT_EXPORT_DAYS, ge=1, le=_MAX_EXPORT_DAYS),
) -> StreamingResponse | JSONResponse:
    """Export CRAT audit chain blocks for a tenant as CSV or JSON.

    Query params:
    - tenant_id: tenant namespace (default = global chain)
    - format: "csv" or "json"
    - days: last N days to include (1–90, default 30)
    """
    redis = _get_redis(request)
    tenant_id = _effective_tenant(request, tenant_id)
    blocks = await _fetch_blocks(redis, tenant_id, days)

    if format == "json":
        payload = [_block_to_row(b) for b in blocks]
        return JSONResponse(content={"tenant_id": tenant_id, "days": days, "total": len(payload), "blocks": payload})

    # CSV export
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for block in blocks:
        writer.writerow(_block_to_row(block))

    csv_bytes = buf.getvalue().encode("utf-8")
    filename = f"crat-{tenant_id}-{time.strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats")
async def crat_stats(
    request: Request,
    tenant_id: str = Query(default="default", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"),
) -> JSONResponse:
    """Return audit chain statistics for a tenant.

    Includes: total_blocks, date_range, event_type_counts, has_signature, chain_valid.
    """
    redis = _get_redis(request)
    tenant_id = _effective_tenant(request, tenant_id)
    key = _blocks_key(tenant_id)

    try:
        raw_list: list[str] = await redis.lrange(key, 0, -1)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis read error: {exc}") from exc

    blocks: list[dict[str, Any]] = [b for r in raw_list if (b := _parse_block(r)) is not None]

    event_type_counts: dict[str, int] = {}
    signed_count = 0
    timestamps: list[str] = []
    chain_valid = True

    prev_hash: str | None = None
    for block in blocks:
        et = block.get("event_type", "UNKNOWN")
        event_type_counts[et] = event_type_counts.get(et, 0) + 1
        if block.get("signature_hex"):
            signed_count += 1
        ts = block.get("timestamp_utc")
        if ts:
            timestamps.append(ts)
        # Quick integrity check: prev_hash chain linkage
        if prev_hash is not None and block.get("prev_hash") != prev_hash:
            chain_valid = False
        prev_hash = block.get("block_hash")

    return JSONResponse(content={
        "tenant_id": tenant_id,
        "total_blocks": len(blocks),
        "date_range": {
            "earliest": min(timestamps) if timestamps else None,
            "latest": max(timestamps) if timestamps else None,
        },
        "event_type_counts": event_type_counts,
        "has_signature": signed_count > 0,
        "signature_coverage": round(signed_count / len(blocks), 4) if blocks else 0.0,
        "chain_valid": chain_valid,
    })
