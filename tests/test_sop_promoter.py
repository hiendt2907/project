"""Tests for S2.2 — Auto-Promote SOP Pipeline."""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.learning_promoter.promoter import evaluate_for_promotion


def _make_ctx(redis_mock, settings_overrides=None):
    ws = SimpleNamespace(
        omni_sop_auto_promote_enabled=True,
        omni_sop_promotion_min_success=3,
        omni_sop_promotion_max_fp_rate=0.05,
        omni_sop_promotion_require_fp_data=True,
        omni_sop_skill_export_dir="",
        embed_model="nomic-embed-text",
        kafka_topic_audit_chain="omni-audit-chain",
    )
    if settings_overrides:
        for k, v in settings_overrides.items():
            setattr(ws, k, v)
    return SimpleNamespace(
        redis=redis_mock,
        kafka=AsyncMock(),
        settings=ws,
        llm=AsyncMock(),
        vector_store=AsyncMock(),
    )


class TestEvaluateForPromotion:
    @pytest.mark.asyncio
    async def test_below_min_success_no_promotion(self):
        redis = AsyncMock()
        redis.hincrby.return_value = 1  # First success — below threshold of 3
        redis.hget.return_value = None
        ctx = _make_ctx(redis)

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="pat-001",
            trace_id="t1",
            tool_name="k8s_scale_deployment",
            match_text="nginx OOM",
            args_playbook={"replicas": 2},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_exact_threshold_triggers_promotion(self):
        redis = AsyncMock()
        redis.hincrby.return_value = 3  # Exactly at threshold
        redis.hget.return_value = None  # Not already promoted

        ctx = _make_ctx(redis)
        ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
        ctx.vector_store.upsert = AsyncMock()

        # KPI evidence present and clean — the quality gate must let this through.
        # (Leaving it absent now blocks promotion by design; that case is covered by
        # test_promotion_blocked_without_fp_evidence.)
        with patch("services.learning_promoter.promoter._get_fp_rate",
                   new=AsyncMock(return_value=0.0)), \
             patch("services.learning_promoter.promoter._write_promo_crat",
                   new=AsyncMock()):
            result = await evaluate_for_promotion(
                ctx,
                pattern_key="pat-002",
                trace_id="t2",
                tool_name="k8s_scale_deployment",
                match_text="nginx OOM",
                args_playbook={"replicas": 2},
            )

        assert result is True
        ctx.vector_store.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_promoted_returns_false(self):
        redis = AsyncMock()
        redis.hincrby.return_value = 5
        redis.hget.return_value = b"sop-existing-id"  # Already promoted!
        ctx = _make_ctx(redis)

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="pat-003",
            trace_id="t3",
            tool_name="k8s_patch_configmap",
            match_text="configmap missing",
            args_playbook={"key": "DB_URL"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_disabled_by_settings_returns_false(self):
        redis = AsyncMock()
        ctx = _make_ctx(redis, {"omni_sop_auto_promote_enabled": False})

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="pat-004",
            trace_id="t4",
            tool_name="k8s_scale_deployment",
            match_text="test",
            args_playbook={},
        )
        assert result is False
        redis.hincrby.assert_not_called()

    @pytest.mark.asyncio
    async def test_high_fp_rate_blocks_promotion(self):
        redis = AsyncMock()
        redis.hincrby.return_value = 10
        redis.hget.return_value = None
        # Simulate 50% FP rate — well above 5% threshold
        redis.zcount.side_effect = [20, 10, 30]  # accepted, rejected, fp

        ctx = _make_ctx(redis)

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="pat-005",
            trace_id="t5",
            tool_name="k8s_scale_deployment",
            match_text="test",
            args_playbook={},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_pattern_key_returns_false(self):
        redis = AsyncMock()
        ctx = _make_ctx(redis)

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="",
            trace_id="t6",
            tool_name="k8s_scale_deployment",
            match_text="test",
            args_playbook={},
        )
        assert result is False
        redis.hincrby.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_failure_returns_false(self):
        redis = AsyncMock()
        redis.hincrby.side_effect = Exception("redis down")
        ctx = _make_ctx(redis)

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="pat-007",
            trace_id="t7",
            tool_name="k8s_scale_deployment",
            match_text="test",
            args_playbook={},
        )
        assert result is False  # Must not raise

    @pytest.mark.asyncio
    async def test_promotion_blocked_without_fp_evidence(self):
        """FAIL_CLOSED: promotion sets auto_execute=True, so it must refuse to run
        when the KPI z-sets hold too few samples to compute a false-positive rate.

        Those z-sets were permanently empty in production (reader/writer key
        mismatch, fixed 2026-07-29), so the old fail-open path was the only path
        ever taken — every candidate was promoted with zero quality evidence."""
        redis = AsyncMock()
        redis.hincrby.return_value = 3
        redis.hget.return_value = None
        redis.zcount.return_value = 0  # no KPI samples at all

        ctx = _make_ctx(redis)

        result = await evaluate_for_promotion(
            ctx,
            pattern_key="pat-008",
            trace_id="t8",
            tool_name="k8s_scale_deployment",
            match_text="nginx OOM",
            args_playbook={"replicas": 2},
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_skill_export_writes_to_configured_dir(self, tmp_path):
        """Export target comes from settings, never a hardcoded workspace path.

        The previous implementation wrote to an absolute developer path
        (/Users/<user>/project/.cursor/skills/learned) which does not exist in the
        worker pod and made a running service write into a source tree."""
        import os
        redis = AsyncMock()
        redis.hincrby.return_value = 3
        redis.hget.return_value = None
        redis.zcount.return_value = 50  # enough samples; fp count also 50 -> see below

        export_dir = tmp_path / "skills"
        ctx = _make_ctx(redis, {
            "omni_sop_skill_export_dir": str(export_dir),
            "omni_sop_promotion_require_fp_data": False,
        })
        ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
        ctx.vector_store.upsert = AsyncMock()

        with patch("services.learning_promoter.promoter._get_fp_rate",
                   new=AsyncMock(return_value=0.0)), \
             patch("services.learning_promoter.promoter._write_promo_crat",
                   new=AsyncMock()):
            result = await evaluate_for_promotion(
                ctx,
                pattern_key="pat-008",
                trace_id="t8",
                tool_name="k8s_scale_deployment",
                match_text="nginx OOM",
                args_playbook={"replicas": 2},
            )

        assert result is True
        target = export_dir / "auto-sop-pat-008.md"
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "auto-sop-pat-008" in content
        assert "nginx OOM" in content
        assert "k8s_scale_deployment" in content

    @pytest.mark.asyncio
    async def test_skill_export_disabled_by_default(self, tmp_path):
        """Empty export dir = feature off; nothing is written anywhere."""
        redis = AsyncMock()
        redis.hincrby.return_value = 3
        redis.hget.return_value = None

        ctx = _make_ctx(redis)  # omni_sop_skill_export_dir="" by default
        ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
        ctx.vector_store.upsert = AsyncMock()

        with patch("services.learning_promoter.promoter._get_fp_rate",
                   new=AsyncMock(return_value=0.0)), \
             patch("services.learning_promoter.promoter._write_promo_crat",
                   new=AsyncMock()):
            result = await evaluate_for_promotion(
                ctx,
                pattern_key="pat-009",
                trace_id="t9",
                tool_name="k8s_scale_deployment",
                match_text="nginx OOM",
                args_playbook={"replicas": 2},
            )

        assert result is True
        assert list(tmp_path.iterdir()) == []

