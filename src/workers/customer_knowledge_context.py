"""Customer-knowledge summary block for advisory evidence (gap 4, Phase 2
omni-close-autonomous-sre-gaps-2026-07-23).

``ingest_customer_knowledge`` (``services.knowledge.document_store``) đã lưu
metadata + summary (<=2000 chars) của tài liệu nghiệp vụ khách hàng, nhưng chưa có
consumer nào đọc lại trước khi LLM ra advisory — tài liệu lưu mà không ai đọc.

Block này render các summary gần nhất (metadata/summary ONLY — INV_DATA_RESIDENCY,
KHÔNG bao giờ kéo full content) thành vài dòng compact, capped theo ``max_chars`` để
không chiếm budget evidence, fail-open thành chuỗi rỗng khi store trống/Redis lỗi
(giống pattern ``workers.system_twin_context``).

Verify-before-believe / INV_LLM_NOT_FIRST: tài liệu khách hàng KHÔNG tự động là Fact
đã verify. Header đánh dấu rõ nguồn "customer-provided, chưa verify" — advisory
reasoning không được coi ngang hàng với bằng chứng probe thật.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 800
_MAX_DOCS = 5
_SUMMARY_PREVIEW_CHARS = 300
_HEADER = (
    "=== CUSTOMER-PROVIDED KNOWLEDGE (unverified · tenant={tenant}) ===\n"
    "Nguồn: tài liệu khách hàng upload qua Telegram, CHƯA qua verify — coi là gợi ý "
    "tham khảo (customer-provided, chưa verify), KHÔNG phải bằng chứng probe đã kiểm "
    "chứng. KHÔNG dùng làm root_cause duy nhất nếu live evidence không xác nhận."
)


async def build_customer_knowledge_block(
    redis: Any, tenant_id: str, max_chars: int = _DEFAULT_MAX_CHARS
) -> str:
    """Render tenant's most recent customer-knowledge doc summaries as a compact,
    explicitly-unverified evidence block.

    Returns "" when there are no docs, or on any store-read failure — customer
    knowledge is supplementary context, never a blocking dependency for advisory.
    """
    try:
        # Local import mirrors workers.system_twin_context: keep the domain-store
        # coupling out of module import time.
        from services.knowledge.document_store import list_docs

        docs = await list_docs(redis, tenant_id=tenant_id, limit=_MAX_DOCS)
    except Exception:  # noqa: BLE001 — supplementary, must never block advisory
        logger.warning(
            "event=customer_knowledge_block_unavailable tenant=%s", tenant_id, exc_info=True
        )
        return ""

    if not docs:
        return ""

    lines = [_HEADER.format(tenant=tenant_id)]
    for doc in docs:
        file_name = str(doc.get("file_name") or "unknown")[:200]
        summary = str(doc.get("summary") or "")[:_SUMMARY_PREVIEW_CHARS]
        if not summary:
            continue
        lines.append(f"- [{file_name}] {summary}")

    if len(lines) == 1:
        return ""  # every doc lacked a usable summary

    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block
