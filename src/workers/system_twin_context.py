"""System Twin summary block for advisory evidence (gap "liên kết" — P1 2026-07-15).

Omni đã có bản đồ hệ thống khách hàng (``omni:aoip:system_model:{tenant}``) nhưng
bộ não advisory chưa bao giờ được xem nó — impact_chain phải đoán dependency.
Block này render tập Fact thành vài dòng compact (grouped theo subject), capped
theo ``max_chars`` để không chiếm budget evidence, và fail-open thành chuỗi rỗng
khi store trống hoặc Redis lỗi (twin là ngữ cảnh bổ trợ, không phải điều kiện).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 800
_HEADER = "=== SYSTEM TWIN (verified facts · tenant={tenant}) ==="


async def build_system_twin_block(
    redis: Any, tenant_id: str, max_chars: int = _DEFAULT_MAX_CHARS
) -> str:
    """Render the tenant's System Twin as a compact evidence block.

    Returns "" when the twin is empty or unreadable — advisory continues
    without it.
    """
    try:
        # Local import: workers may depend on aoip domain reads (precedent:
        # onboarding_pipeline), but keep the coupling out of module import time.
        from aoip.system_model_store import load_system_model

        model, _revision = await load_system_model(redis, tenant_id)
    except Exception:  # noqa: BLE001 — twin là bổ trợ, không được chặn advisory
        logger.warning(
            "event=system_twin_block_unavailable tenant=%s", tenant_id, exc_info=True
        )
        return ""

    if not model.facts:
        return ""

    grouped: dict[str, list[str]] = {}
    for fact in model.facts:
        grouped.setdefault(fact.subject, []).append(f"{fact.predicate} {fact.obj}")

    lines = [_HEADER.format(tenant=tenant_id)]
    for subject in sorted(grouped):
        lines.append(f"{subject}: " + " · ".join(grouped[subject]))

    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block
