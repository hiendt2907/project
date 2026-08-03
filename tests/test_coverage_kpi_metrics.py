"""Coverage tests for src/workers/kpi_metrics.py — KPIStore and _handle_feedback."""
from __future__ import annotations

import time
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch

from workers.kpi_metrics import KPIStore, _handle_feedback


@pytest.fixture
async def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
async def store(redis):
    return KPIStore(redis)


# ── KPIStore.record_accepted ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_accepted_adds_to_zadd(store, redis):
    await store.record_accepted("trace-001")
    count = await redis.zcount("omni:kpi:z:default:accepted", "-inf", "+inf")
    assert count == 1


@pytest.mark.asyncio
async def test_record_accepted_multiple(store, redis):
    await store.record_accepted("trace-001")
    await store.record_accepted("trace-002")
    await store.record_accepted("trace-003")
    count = await redis.zcount("omni:kpi:z:default:accepted", "-inf", "+inf")
    assert count == 3


# ── KPIStore.record_rejected ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_rejected_adds_to_set(store, redis):
    await store.record_rejected("trace-r01")
    count = await redis.zcount("omni:kpi:z:default:rejected", "-inf", "+inf")
    assert count == 1


# ── KPIStore.record_false_positive ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_false_positive(store, redis):
    await store.record_false_positive("trace-fp01")
    count = await redis.zcount("omni:kpi:z:default:false_positive", "-inf", "+inf")
    assert count == 1


# ── KPIStore.record_detected / record_resolved ────────────────────────────────

@pytest.mark.asyncio
async def test_record_detected_stores_score(store, redis):
    """Khoá KPI nhóm theo DOMAIN, không theo lane trục A (đổi 2026-07-30)."""
    ts = time.time()
    await store.record_detected("trace-d01", "SYS_RESOURCE", ts)
    count = await redis.zcount("omni:kpi:detected:default:os_host", "-inf", "+inf")
    assert count == 1


@pytest.mark.asyncio
async def test_record_resolved_stores_score(store, redis):
    ts = time.time()
    await store.record_resolved("trace-r01", "APP_HTTP", ts)
    count = await redis.zcount("omni:kpi:resolved:default:application", "-inf", "+inf")
    assert count == 1


@pytest.mark.asyncio
async def test_record_accepts_both_lane_vocabularies(store, redis):
    """Trường `lane` của feedback mang trục A ở nơi này, trục B ở nơi khác.

    Chuẩn hoá phải nhận cả hai, nếu không một nửa số liệu KPI rơi vào `unknown` mà
    không ai thấy.
    """
    ts = time.time()
    await store.record_detected("trace-a", "SIEM_SECURITY", ts)   # trục A
    await store.record_detected("trace-b", "siem", ts)            # trục B / alias
    count = await redis.zcount("omni:kpi:detected:default:security", "-inf", "+inf")
    assert count == 2


@pytest.mark.asyncio
async def test_sys_hard_fail_lands_in_unknown_not_guessed(store, redis):
    """SYS_HARD_FAIL gánh 4 domain — `unknown` là câu trả lời trung thực."""
    ts = time.time()
    await store.record_detected("trace-hf", "SYS_HARD_FAIL", ts)
    assert await redis.zcount("omni:kpi:detected:default:unknown", "-inf", "+inf") == 1
    for guessed in ("database", "storage", "service", "kubernetes"):
        assert await redis.zcount(f"omni:kpi:detected:default:{guessed}", "-inf", "+inf") == 0


# ── KPIStore.get_summary ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_summary_empty(store):
    summary = await store.get_summary()
    assert summary["accepted"] == 0
    assert summary["rejected"] == 0
    assert summary["false_positive"] == 0
    assert summary["acceptance_rate"] is None
    assert summary["false_positive_rate"] is None


@pytest.mark.asyncio
async def test_get_summary_with_data(store):
    await store.record_accepted("t1")
    await store.record_accepted("t2")
    await store.record_rejected("t3")
    summary = await store.get_summary()
    assert summary["accepted"] == 2
    assert summary["rejected"] == 1
    assert abs(summary["acceptance_rate"] - (2 / 3)) < 0.01


@pytest.mark.asyncio
async def test_get_summary_acceptance_rate_all_accepted(store):
    await store.record_accepted("t1")
    await store.record_accepted("t2")
    summary = await store.get_summary()
    assert summary["acceptance_rate"] == 1.0
    # false_positive_rate = false_pos(0) / total_executed(accepted=2) = 0.0
    assert summary["false_positive_rate"] == 0.0


@pytest.mark.asyncio
async def test_get_summary_false_positive_rate(store):
    await store.record_accepted("t1")
    await store.record_false_positive("t2")
    summary = await store.get_summary()
    # false_positive_rate = false_pos / total_executed (accepted)
    assert summary["false_positive_rate"] is not None


@pytest.mark.asyncio
async def test_get_summary_window_seconds(store):
    summary = await store.get_summary()
    assert summary["window_seconds"] == 86400


# ── _handle_feedback ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_feedback_success_outcome(redis):
    store = KPIStore(redis)
    with patch("workers.kpi_metrics.KPIStore", return_value=store):
        with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
             patch("workers.metrics_exporter.observe_kpi_mttr") as mock_mttr, \
             patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate") as mock_accept, \
             patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
            fields = {
                "outcome": "success",
                "trace_id": "trace-success-01",
                "lane": "SYS_RESOURCE",
                "detected_at": str(time.time() - 60),
                "resolved_at": str(time.time()),
            }
            await _handle_feedback(store, fields)
            mock_inc.assert_called_once_with("SYS_RESOURCE", "accepted")
            mock_mttr.assert_called_once()
            mock_accept.assert_called_once()


@pytest.mark.asyncio
async def test_handle_feedback_approved_outcome(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
         patch("workers.metrics_exporter.observe_kpi_mttr"), \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {"outcome": "APPROVED", "trace_id": "t2", "lane": "APP_HTTP"}
        await _handle_feedback(store, fields)
        mock_inc.assert_called_once_with("APP_HTTP", "accepted")


@pytest.mark.asyncio
async def test_handle_feedback_rejected_outcome(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {"outcome": "rejected", "trace_id": "t3", "lane": "SIEM_SECURITY"}
        await _handle_feedback(store, fields)
        mock_inc.assert_called_once_with("SIEM_SECURITY", "rejected")


@pytest.mark.asyncio
async def test_handle_feedback_REJECTED_outcome(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {"outcome": "REJECTED", "trace_id": "t4", "lane": "SYS_HARD_FAIL"}
        await _handle_feedback(store, fields)
        mock_inc.assert_called_once_with("SYS_HARD_FAIL", "rejected")


@pytest.mark.asyncio
async def test_handle_feedback_executor_fail_outcome(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {"outcome": "executor_fail", "trace_id": "t5", "lane": "SYS_RESOURCE"}
        await _handle_feedback(store, fields)
        mock_inc.assert_called_once_with("SYS_RESOURCE", "false_positive")


@pytest.mark.asyncio
async def test_handle_feedback_fail_outcome(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {"outcome": "fail", "trace_id": "t6", "lane": "APP_HTTP"}
        await _handle_feedback(store, fields)
        mock_inc.assert_called_once_with("APP_HTTP", "false_positive")


@pytest.mark.asyncio
async def test_handle_feedback_unknown_outcome_no_record(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident") as mock_inc, \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {"outcome": "unknown_xyz", "trace_id": "t7", "lane": "APP_HTTP"}
        await _handle_feedback(store, fields)
        mock_inc.assert_not_called()


@pytest.mark.asyncio
async def test_handle_feedback_no_detected_at_skips_mttd(redis):
    """When detected_at is 0 or missing, MTTR observation is skipped."""
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident"), \
         patch("workers.metrics_exporter.observe_kpi_mttr") as mock_mttr, \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate"), \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        fields = {
            "outcome": "verified",
            "trace_id": "t8",
            "lane": "SYS_RESOURCE",
            "detected_at": "0",
        }
        await _handle_feedback(store, fields)
        mock_mttr.assert_not_called()


@pytest.mark.asyncio
async def test_handle_feedback_updates_acceptance_rate_after_mix(redis):
    store = KPIStore(redis)
    with patch("workers.metrics_exporter.inc_kpi_incident"), \
         patch("workers.metrics_exporter.observe_kpi_mttr"), \
         patch("workers.metrics_exporter.set_kpi_advisory_acceptance_rate") as mock_accept, \
         patch("workers.metrics_exporter.set_kpi_false_positive_rate"):
        # 2 accepted, 1 rejected → rate = 2/3
        for i in range(2):
            await _handle_feedback(store, {"outcome": "success", "trace_id": f"ok-{i}", "lane": "SYS_RESOURCE"})
        await _handle_feedback(store, {"outcome": "rejected", "trace_id": "rej-1", "lane": "SYS_RESOURCE"})
        # Last call should set rate
        last_call = mock_accept.call_args_list[-1]
        rate = last_call[0][0]
        assert abs(rate - 2 / 3) < 0.01
