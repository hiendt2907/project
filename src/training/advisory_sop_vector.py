"""Backfill advisory hash ``omni:rag:sop:{tenant}`` → HNSW ``itops_sop_ledger``.

``advisory_ingest`` chỉ ghi Redis hash phẳng; vector search (``redis_brain``,
``resolve_remediation_from_memory``) query HNSW collection nên toàn bộ advisory
đã ingest trước đây vô hình với RAG. Module này nạp chúng vào đúng collection.

INVARIANT an toàn: payload luôn ``auto_execute=False`` và không có ``tool`` —
advisory JSONL mô tả remediation bằng văn xuôi, KHÔNG map sang ``TOOL_REGISTRY``,
nên không được phép kích hoạt fast-path tự thực thi. Chúng chỉ là knowledge
context cho LLM.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from rag.redis_vector_store import DEFAULT_TENANT_ID, PointStruct
from rag.sop_ledger import SOP_COLLECTION

logger = logging.getLogger(__name__)

SOP_HASH_KEY_FMT = "omni:rag:sop:{tenant_id}"
EMBED_BATCH = 32
MATCH_TEXT_MAX = 8000

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def advisory_match_text(entry: dict[str, Any]) -> str:
    """Text dùng để embed — gộp tín hiệu nhận dạng sự cố, không gộp remediation."""
    ctx = entry.get("alert_context") or {}
    annotations = ctx.get("annotations") or {}
    parts = [
        str(entry.get("lane") or ""),
        str(ctx.get("alertname") or ""),
        str(ctx.get("severity") or ""),
        str(annotations.get("summary") or ""),
        str(entry.get("root_cause") or ""),
    ]
    evidence = entry.get("evidence")
    if isinstance(evidence, list):
        parts.extend(str(e) for e in evidence[:3])
    return "\n".join(p for p in parts if p.strip())[:MATCH_TEXT_MAX]


def advisory_sop_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Payload read-only cho ``redis_brain``; không bao giờ auto-execute."""
    ctx = entry.get("alert_context") or {}
    return {
        "sop_id": str(entry.get("alert_id") or ""),
        "lane": str(entry.get("lane") or ""),
        "alertname": str(ctx.get("alertname") or ""),
        "match_text": advisory_match_text(entry),
        "root_cause": str(entry.get("root_cause") or ""),
        "source": "advisory_backfill",
        "auto_execute": False,
    }


def _point_id(tenant_id: str, alert_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"advisory-sop:{tenant_id}:{alert_id}"))


def _parse_entries(raw_map: dict[str, str]) -> list[dict[str, Any]]:
    """Bỏ qua bản ghi hỏng/rỗng thay vì làm hỏng cả lượt backfill."""
    entries: list[dict[str, Any]] = []
    for field, raw in raw_map.items():
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError) as e:
            logger.warning("skip malformed advisory field=%s err=%s", field, e)
            continue
        if not isinstance(obj, dict) or not obj.get("alert_id"):
            logger.warning("skip advisory without alert_id field=%s", field)
            continue
        if not advisory_match_text(obj).strip():
            logger.warning("skip advisory with empty match_text alert_id=%s", obj["alert_id"])
            continue
        entries.append(obj)
    return entries


async def backfill_sop_vectors(
    *,
    redis: Any,
    store: Any,
    embed_fn: EmbedFn,
    tenant_id: str = DEFAULT_TENANT_ID,
    dry_run: bool = False,
    embed_batch: int = EMBED_BATCH,
) -> int:
    """Đọc hash advisory → embed → upsert HNSW. Trả về số entry hợp lệ."""
    raw_map = await redis.hgetall(SOP_HASH_KEY_FMT.format(tenant_id=tenant_id))
    entries = _parse_entries(raw_map or {})
    if not entries:
        logger.info("advisory_sop_backfill: nothing to do tenant_id=%s", tenant_id)
        return 0

    if not dry_run:
        await store.ensure_ready()

    for i in range(0, len(entries), embed_batch):
        chunk = entries[i : i + embed_batch]
        vectors = await embed_fn([advisory_match_text(e) for e in chunk])
        if len(vectors) != len(chunk):
            raise RuntimeError(f"embed batch mismatch: want {len(chunk)} got {len(vectors)}")
        if dry_run:
            continue
        points = [
            PointStruct(
                id=_point_id(tenant_id, str(e["alert_id"])),
                vector=vec,
                payload=advisory_sop_payload(e),
            )
            for e, vec in zip(chunk, vectors, strict=True)
        ]
        await store.upsert(SOP_COLLECTION, points, tenant_id=tenant_id)

    logger.info(
        "advisory_sop_backfill complete tenant_id=%s entries=%d dry_run=%s",
        tenant_id, len(entries), dry_run,
    )
    return len(entries)
