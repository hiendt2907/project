"""P0 #3 — ngưỡng "agent còn online" phải SUY RA từ registry TTL, không phải hằng số rời.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #3): lần thứ 5
trong dự án gặp đúng lớp bug "ngưỡng gõ cứng lệch config nó phụ thuộc" (trước:
domain/signal_kind/_domain/snapshot_freshness). `_AGENT_ONLINE_MAX_AGE_S` từng là số 120
gõ tay, độc lập với `agent_push._REGISTRY_TTL` (=300 thật) — comment cũ còn ghi sai "gateway
TTL=120s". Nếu registry TTL được tune (vd giảm xuống 60 để dọn Redis nhanh hơn), ngưỡng
online-check cũ sẽ không bao giờ hết hạn trước khi registry key tự biến mất, làm mất hẳn ý
nghĩa của bước kiểm tra "còn online gần đây" — degrade âm thầm.
"""

from __future__ import annotations

import re

import services.analyst.diagnosis_loop as dl


def test_online_threshold_is_a_fraction_of_registry_ttl_not_a_bare_literal() -> None:
    assert dl._AGENT_ONLINE_MAX_AGE_S == dl._AGENT_REGISTRY_TTL_SEC / 2
    assert dl._AGENT_ONLINE_MAX_AGE_S == 150.0


def test_registry_ttl_source_matches_agent_push_single_source_of_truth() -> None:
    """Đúng 1 nguồn: TTL này phải đến từ agent_push._REGISTRY_TTL, không phải bản sao chép."""
    from gateway.routes.agent_push import _REGISTRY_TTL as _real_ttl

    assert dl._AGENT_REGISTRY_TTL_SEC == _real_ttl


def test_diagnosis_loop_source_does_not_hardcode_the_old_literal() -> None:
    """Chặn regression: không được gõ lại 120 làm hằng số online-check độc lập."""
    src = open("src/services/analyst/diagnosis_loop.py", encoding="utf-8").read()
    body = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"_AGENT_ONLINE_MAX_AGE_S\s*=\s*120\b", body)
    assert "_AGENT_REGISTRY_TTL_SEC" in body
