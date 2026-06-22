"""RAG upsert gate — legacy finalize vs SDK-verified."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.autonomous_feedback_loop import _finalize_feedback_success_legacy


@pytest.mark.asyncio
async def test_legacy_finalize_skips_experience_when_requires_sdk_verify():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_experience_requires_sdk_verify=True)
    ctx.redis = AsyncMock()
    ctx.redis.delete = AsyncMock()

    with (
        patch("workers.autonomous_feedback_loop._archive_postmortem"),
        patch("workers.autonomous_feedback_loop._upsert_action_experience_on_success", new_callable=AsyncMock) as up,
        patch("workers.autonomous_feedback_loop._write_success_hot_cache", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop.emit_transition", new_callable=AsyncMock),
    ):
        await _finalize_feedback_success_legacy(
            ctx,
            trace="t-gate",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={"namespace": "n"},
            stdout="ok",
            ctx_obj={},
        )
    up.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_finalize_upserts_when_experience_not_requires_sdk_verify():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_experience_requires_sdk_verify=False)
    ctx.redis = AsyncMock()
    ctx.redis.delete = AsyncMock()

    with (
        patch("workers.autonomous_feedback_loop._archive_postmortem"),
        patch("workers.autonomous_feedback_loop._upsert_action_experience_on_success", new_callable=AsyncMock) as up,
        patch("workers.autonomous_feedback_loop._write_success_hot_cache", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop.emit_transition", new_callable=AsyncMock),
    ):
        await _finalize_feedback_success_legacy(
            ctx,
            trace="t-gate2",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={"namespace": "n"},
            stdout="ok",
            ctx_obj={},
        )
    up.assert_called_once()
