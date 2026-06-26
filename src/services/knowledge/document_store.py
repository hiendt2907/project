"""Knowledge document store — lưu tài liệu khách hàng upload qua Telegram.

INV_DATA_RESIDENCY: tài liệu thuộc về khách hàng, Omni chỉ lưu ánh xạ ngắn (metadata).
Nội dung tài liệu KHÔNG lưu nguyên văn trên Redis Omni — chỉ lưu file_id Telegram
+ summary ngắn (<=2000 ký tự) để tra cứu RAG. Xem memory project_data_residency_onboarding_agent.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DOC_KEY_PREFIX = "omni:knowledge:doc:"
_DOC_INDEX_KEY = "omni:knowledge:doc_index:{tenant_id}"
_DOC_TTL = 90 * 86400  # 90 ngày


async def ingest_customer_knowledge(
    redis: Any,
    *,
    tenant_id: str,
    agent_id: str,
    file_id: str,
    file_name: str,
    summary: str,
    uploaded_by: str = "telegram",
) -> str:
    """Lưu metadata tài liệu khách hàng. Trả về doc_id.

    summary: tóm tắt ngắn (<= 2000 ký tự) do LLM hoặc admin cung cấp.
    file_id: Telegram file_id để tải lại nếu cần.
    """
    doc_id = f"{tenant_id}:{agent_id}:{int(time.time())}"
    doc = {
        "doc_id": doc_id,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "file_id": file_id,
        "file_name": file_name[:200],
        "summary": summary[:2000],
        "uploaded_by": uploaded_by,
        "created_at": int(time.time()),
    }
    try:
        key = f"{_DOC_KEY_PREFIX}{doc_id}"
        await redis.set(key, json.dumps(doc, ensure_ascii=False), ex=_DOC_TTL)
        index_key = _DOC_INDEX_KEY.format(tenant_id=tenant_id)
        await redis.lpush(index_key, doc_id)
        await redis.expire(index_key, _DOC_TTL)
        logger.info("knowledge: doc ingested doc_id=%s tenant=%s file=%s", doc_id, tenant_id, file_name)
    except Exception as exc:
        logger.warning("knowledge: ingest_doc failed doc_id=%s err=%r", doc_id, exc)
    return doc_id


async def get_doc(redis: Any, *, doc_id: str) -> dict[str, Any] | None:
    try:
        raw = await redis.get(f"{_DOC_KEY_PREFIX}{doc_id}")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("knowledge: get_doc failed doc_id=%s err=%r", doc_id, exc)
        return None


async def list_docs(redis: Any, *, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        index_key = _DOC_INDEX_KEY.format(tenant_id=tenant_id)
        doc_ids = await redis.lrange(index_key, 0, limit - 1)
        docs = []
        for doc_id in doc_ids:
            d = await get_doc(redis, doc_id=doc_id)
            if d:
                docs.append(d)
        return docs
    except Exception as exc:
        logger.warning("knowledge: list_docs failed tenant=%s err=%r", tenant_id, exc)
        return []
