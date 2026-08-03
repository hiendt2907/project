"""Phản xạ nhanh cho remote-agent (VM khách) — điểm đối xứng của
`proactive_observer.py` cho host KHÔNG có Prometheus để cào.

Trigger không phải PromQL threshold cross (không có gì để cào phía khách) mà
là chính deviation z-score/ngưỡng tĩnh Omni đã tự tính trong
`knowledge_pipeline._decide_and_promote()` — tín hiệu đó vốn đã thuần số,
không cần thêm hạ tầng gì phía khách.

Thực thi KHÔNG thể tái dùng `resolve_known_fix()` (gọi hàm Python tại chỗ) vì
VM khách không có API trong-process nào để gọi — phải qua kênh lệnh bền
(gateway HTTP enqueue → agent tự poll → thực thi → báo kết quả qua
`remote_command_outcome_loop.py`). Module này chỉ nối `find_known_fix_candidate`
(tìm + 2 lớp guard) với `auto_recovery_bridge.dispatch_if_eligible` (thực thi
đã có sẵn CRAT fail-closed + allowlist blast-radius + đăng ký reconcile) —
không viết lại cơ chế dispatch mới, không mở thêm bề mặt rủi ro nào ngoài
những gì `dispatch_if_eligible` đã kiểm (agent phải nằm trong
`OMNI_LAB_AUTO_EXECUTE_AGENTS`, confidence >= 0.75, CRAT ghi trước khi phát).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def try_remote_known_fix(
    ctx: Any,
    *,
    query_text: str,
    score_threshold: float,
    host_scope: frozenset[str] | None,
    agent_id: str,
    tenant_id: str,
    trace_id: str,
) -> dict[str, Any]:
    """Tìm một cách sửa đã biết cho `query_text`, dispatch qua kênh lệnh bền
    nếu qua được cả hai lớp guard VÀ `dispatch_if_eligible` chấp nhận (allowlist
    + confidence). Trả `{"resolved": bool, "reason": str, ...}` — `resolved`
    KHÔNG có nghĩa "đã sửa xong", chỉ có nghĩa "đã phát lệnh thành công"; kết
    quả thật đến sau qua `remote_command_outcome_loop`.
    """
    from pkg.reasoning.known_fix_resolver import find_known_fix_candidate
    from workers.auto_recovery_bridge import _SUPPORTED_CAPABILITIES, dispatch_if_eligible

    candidate, reason = await find_known_fix_candidate(
        ctx,
        query_text=query_text,
        score_threshold=score_threshold,
        host_scope=host_scope,
        valid_tools=_SUPPORTED_CAPABILITIES,
    )
    if candidate is None:
        return {"resolved": False, "reason": reason}

    unit = str(candidate.args.get("unit") or "")
    if not unit:
        # Guard hẹp cho case này: capability hợp lệ nhưng thiếu chính đối số
        # `extract_suggested_recovery` bắt buộc — coi như không có ứng viên,
        # không cố đoán unit từ đâu khác.
        logger.warning(
            "remote_known_fix: candidate missing 'unit' tool=%s trace=%s", candidate.tool, trace_id
        )
        return {"resolved": False, "reason": "candidate_missing_unit"}

    final = {
        "suggested_recovery": {"capability": candidate.tool, "unit": unit},
        "confidence": candidate.score,
        "root_cause": f"[known-fix reflex] {query_text[:200]}",
    }

    import httpx

    async with httpx.AsyncClient() as client:
        result = await dispatch_if_eligible(
            settings=ctx.settings,
            http_client=client,
            final=final,
            agent_id=agent_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
            redis=ctx.redis,
            kafka=ctx.kafka,
        )

    resolved = bool(result.get("dispatched"))
    if resolved:
        logger.info(
            "remote_known_fix: dispatched trace=%s agent=%s tool=%s unit=%s command_id=%s",
            trace_id, agent_id, candidate.tool, unit, result.get("command_id"),
        )
    else:
        logger.info(
            "remote_known_fix: candidate found but not dispatched trace=%s agent=%s reason=%s",
            trace_id, agent_id, result.get("reason"),
        )
    return {"resolved": resolved, **result}
