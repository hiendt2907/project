"""P0 #2 — cổng meta_self trong _proof_of_fault_gate phải FAIL-CLOSED khi đọc lỗi.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #2): trước fix,
`omni:trace:{trace}:alert_class` bị Redis đọc lỗi (timeout/flaky) hay JSON hỏng thì gate coi
như "không phải meta_self" và cứ để mutate-planner chạy tiếp — đúng lúc
OMNI_AUTO_EXECUTE_ENABLED=true sống thật trên prod, một lỗi Redis thoáng qua đủ tắt lá chắn
này mà không ai biết (log chỉ ở mức debug hoặc hoàn toàn im lặng).

Sau fix: đọc lỗi / JSON hỏng -> chặn mutate (ERR_ALERT_CLASS_READ_FAILED) + WARNING. Key
vắng mặt HỢP LỆ (trace chưa từng qua alert-classify, ví dụ evidence từ remote_agent) vẫn phải
rơi qua bình thường như cũ — không được biến audit này thành "mọi trace không alert đều bị
chặn".
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis

import workers.evidence_consumer as ec


def _ctx(redis: FakeRedis) -> SimpleNamespace:
    return SimpleNamespace(
        redis=redis,
        settings=SimpleNamespace(
            baseline_dr_z_threshold=3.0,
            autonomous_sigma_observation_window=1,
            omni_proof_lane_enabled=True,
        ),
    )


@pytest.mark.asyncio
async def test_redis_read_error_on_alert_class_fails_closed_not_open(caplog) -> None:
    """Đọc omni:trace:{trace}:alert_class ném lỗi -> PHẢI chặn mutate, không được lọt qua."""
    redis = FakeRedis(decode_responses=True)
    ctx = _ctx(redis)

    with (
        patch.object(redis, "get", AsyncMock(side_effect=ConnectionError("redis flaky"))),
        caplog.at_level(logging.WARNING, logger="workers.evidence_consumer"),
    ):
        ok, err, extra = await ec._proof_of_fault_gate(ctx, trace="trace-redis-flaky", batch=[])

    assert ok is False, "đọc lỗi phải chặn mutate (fail-closed), không được coi là an toàn"
    assert err == "ERR_ALERT_CLASS_READ_FAILED"
    assert extra["alert_class"] == "unknown_read_error"
    assert any(
        "alert_class_read_failed" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "phải có WARNING khi đọc alert_class lỗi — không được im lặng"


@pytest.mark.asyncio
async def test_corrupt_alert_class_json_fails_closed(caplog) -> None:
    """Key tồn tại nhưng JSON hỏng -> PHẢI chặn mutate, giống trường hợp đọc lỗi."""
    redis = FakeRedis(decode_responses=True)
    await redis.set("omni:trace:trace-corrupt-ac:alert_class", "not-valid-json{{{")
    ctx = _ctx(redis)

    with caplog.at_level(logging.WARNING, logger="workers.evidence_consumer"):
        ok, err, extra = await ec._proof_of_fault_gate(ctx, trace="trace-corrupt-ac", batch=[])

    assert ok is False
    assert err == "ERR_ALERT_CLASS_READ_FAILED"
    assert extra["alert_class"] == "unknown_corrupt"
    assert any(
        "alert_class_corrupt" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_confirmed_meta_self_still_blocks_with_correct_reason() -> None:
    """Regression: case đã có từ trước (key tồn tại, kind=meta_self) vẫn phải chặn đúng lý do."""
    redis = FakeRedis(decode_responses=True)
    await redis.set(
        "omni:trace:trace-real-meta:alert_class",
        json.dumps({"kind": "meta_self", "mutate_eligible": False, "alertname": "OmniWorkerStalled"}),
    )
    ctx = _ctx(redis)

    ok, err, extra = await ec._proof_of_fault_gate(ctx, trace="trace-real-meta", batch=[])

    assert ok is False
    assert err == "ERR_META_SELF_NO_TARGET"
    assert extra["alert_class"] == "meta_self"


@pytest.mark.asyncio
async def test_missing_key_no_error_falls_through_normally() -> None:
    """Key vắng mặt HỢP LỆ (không phải lỗi đọc) -> KHÔNG bị chặn bởi cổng meta_self.

    Đây là trường hợp phổ biến nhất trong thực tế: evidence không đến từ đường alert
    (ví dụ remote_agent) không bao giờ có key này — audit không được biến case này
    thành false-positive block.
    """
    redis = FakeRedis(decode_responses=True)
    ctx = _ctx(redis)

    with (
        patch.object(ec, "resolve_proof_lane", return_value=("resource", "test")),
        patch.object(ec, "critical_evidence_present", return_value=False),
    ):
        ok, err, extra = await ec._proof_of_fault_gate(ctx, trace="trace-no-alert-class", batch=[])

    # Không bị chặn bởi lý do meta_self/alert_class — có thể vẫn False vì lý do khác
    # (thiếu snapshot/critical evidence), nhưng KHÔNG được là hai mã lỗi fail-closed mới.
    assert err not in ("ERR_META_SELF_NO_TARGET", "ERR_ALERT_CLASS_READ_FAILED")


@pytest.mark.asyncio
async def test_shortcut_read_failure_logs_warning_but_falls_through(caplog) -> None:
    """_handle_meta_self_alert (site B, chỉ là short-circuit tối ưu) vẫn phải log khi lỗi,
    nhưng KHÔNG cần chặn gì — cổng thật là _proof_of_fault_gate ở trên."""
    redis = FakeRedis(decode_responses=True)
    settings = SimpleNamespace(meta_self_alert_cooldown_sec=1800)
    ctx = SimpleNamespace(redis=redis, settings=settings)

    with (
        patch.object(redis, "get", AsyncMock(side_effect=ConnectionError("redis flaky"))),
        caplog.at_level(logging.WARNING, logger="workers.evidence_consumer"),
    ):
        out = await ec._handle_meta_self_alert(ctx, trace="trace-shortcut-flaky")

    assert out is None, "phải fall-through về diagnosis flow đầy đủ, không tự chặn ở đây"
    assert any(
        "meta_self_shortcut_read_failed" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )
