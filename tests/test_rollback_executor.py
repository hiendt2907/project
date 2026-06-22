"""Tests for S1.2 — Pre-Execute Snapshot + Auto-Rollback."""

from __future__ import annotations

import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from workers.rollback_executor import (
    snapshot_required,
    capture_pre_mutate_snapshot,
    apply_rollback_from_snapshot,
)


class TestSnapshotRequired:
    def test_scale_deployment_requires_snapshot(self):
        assert snapshot_required("k8s_scale_deployment") is True

    def test_patch_configmap_requires_snapshot(self):
        assert snapshot_required("k8s_patch_configmap") is True

    def test_create_or_patch_configmap_requires_snapshot(self):
        assert snapshot_required("k8s_create_or_patch_configmap") is True

    def test_patch_resource_requires_snapshot(self):
        assert snapshot_required("k8s_patch_resource") is True

    def test_patch_secret_requires_snapshot(self):
        assert snapshot_required("k8s_patch_secret") is True

    def test_rollout_restart_no_snapshot(self):
        # idempotent — no rollback needed
        assert snapshot_required("k8s_rollout_restart") is False

    def test_readonly_tool_no_snapshot(self):
        assert snapshot_required("k8s_describe_resource") is False
        assert snapshot_required("k8s_get_logs") is False

    def test_unknown_tool_no_snapshot(self):
        assert snapshot_required("nonexistent_tool") is False


class TestCapturePreMutateSnapshot:
    def _make_ctx(self, redis_mock):
        return SimpleNamespace(redis=redis_mock, settings=SimpleNamespace())

    @pytest.mark.asyncio
    async def test_non_snapshot_tool_returns_none(self):
        redis = AsyncMock()
        ctx = self._make_ctx(redis)
        result = await capture_pre_mutate_snapshot(
            ctx, "k8s_rollout_restart", {"namespace": "ns", "name": "dep"}, "trace-001"
        )
        assert result is None
        redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_stores_snapshot_in_redis(self):
        redis = AsyncMock()
        ctx = self._make_ctx(redis)

        with patch("workers.rollback_executor._capture", new=AsyncMock(return_value={"prior_replicas": 3})):
            result = await capture_pre_mutate_snapshot(
                ctx, "k8s_scale_deployment", {"namespace": "ns", "name": "dep", "replicas": 0}, "trace-002"
            )

        assert result is not None
        assert result["tool_name"] == "k8s_scale_deployment"
        assert result["namespace"] == "ns"
        assert result["prior_replicas"] == 3
        redis.setex.assert_called_once()
        call_args = redis.setex.call_args
        assert "omni:rollback:snapshot:trace-002" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_capture_error_recorded_in_snap(self):
        redis = AsyncMock()
        ctx = self._make_ctx(redis)

        with patch("workers.rollback_executor._capture", new=AsyncMock(side_effect=Exception("k8s down"))):
            result = await capture_pre_mutate_snapshot(
                ctx, "k8s_patch_configmap", {"namespace": "ns", "name": "cm", "key": "k"}, "trace-003"
            )

        assert result is not None
        assert "capture_error" in result
        assert "k8s down" in result["capture_error"]
        # Still stores in Redis with error recorded
        redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_raise(self):
        redis = AsyncMock()
        redis.setex.side_effect = Exception("redis down")
        ctx = self._make_ctx(redis)

        with patch("workers.rollback_executor._capture", new=AsyncMock(return_value={})):
            result = await capture_pre_mutate_snapshot(
                ctx, "k8s_patch_configmap", {"namespace": "ns", "name": "cm", "key": "k"}, "trace-004"
            )
        # Should not raise — storage failure is logged but not propagated
        assert result is not None


class TestApplyRollbackFromSnapshot:
    def _make_ctx(self, redis_mock, rollback_target_name: str = "my-deployment"):
        ctx = SimpleNamespace(
            redis=redis_mock,
            settings=SimpleNamespace(
                omni_auto_rollback_enabled=True,
                kafka_topic_audit_chain="omni-audit-chain",
            ),
            kafka=AsyncMock(),
            rollback_target_name=rollback_target_name,
        )
        return ctx

    @pytest.mark.asyncio
    async def test_no_redis_returns_skip(self):
        ctx = SimpleNamespace(redis=None)
        ok, msg = await apply_rollback_from_snapshot(ctx, "trace-001")
        assert ok is False
        assert "no redis" in msg

    @pytest.mark.asyncio
    async def test_no_snapshot_returns_skip(self):
        redis = AsyncMock()
        redis.get.return_value = None
        ctx = self._make_ctx(redis)
        ok, msg = await apply_rollback_from_snapshot(ctx, "trace-nosnap")
        assert ok is False
        assert "no_snapshot" in msg

    @pytest.mark.asyncio
    async def test_secret_tool_returns_notify_only(self):
        redis = AsyncMock()
        snap = {
            "tool_name": "k8s_patch_secret",
            "namespace": "ns",
            "secret_key_name": "DB_PASSWORD",
            "secret_values_not_stored": True,
        }
        redis.get.return_value = json.dumps(snap).encode()
        ctx = self._make_ctx(redis)
        ok, msg = await apply_rollback_from_snapshot(ctx, "trace-secret")
        assert ok is False
        assert "notify_only" in msg

    @pytest.mark.asyncio
    async def test_patch_resource_returns_notify_only(self):
        redis = AsyncMock()
        snap = {
            "tool_name": "k8s_patch_resource",
            "namespace": "ns",
            "patch_keys_affected": ["spec"],
            "prior_spec_capture": "manual_restore_required",
        }
        redis.get.return_value = json.dumps(snap).encode()
        ctx = self._make_ctx(redis)
        with patch("workers.k8s_tools._load_k8s_config", new=AsyncMock()):
            ok, msg = await apply_rollback_from_snapshot(ctx, "trace-patch-res")
        assert ok is False
        assert "notify_only" in msg

    @pytest.mark.asyncio
    async def test_scale_deployment_rollback_success(self):
        redis = AsyncMock()
        snap = {
            "tool_name": "k8s_scale_deployment",
            "namespace": "multi-agent",
            "prior_replicas": 3,
        }
        redis.get.return_value = json.dumps(snap).encode()
        ctx = self._make_ctx(redis, rollback_target_name="nginx")

        mock_dep = MagicMock()
        mock_dep.spec.replicas = 0
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = mock_dep
        mock_apps.replace_namespaced_deployment.return_value = None
        mock_apps.api_client.close = AsyncMock()

        mock_client = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps

        mock_audit = AsyncMock()

        with (
            patch("workers.k8s_tools._load_k8s_config", new=AsyncMock()),
            patch("workers.rollback_executor.client", mock_client),
            patch("services.audit_ledger.chain_writer.write_audit_block", mock_audit),
        ):
            # Directly test _apply function
            from workers.rollback_executor import _apply
            ok, msg = await _apply(ctx, "k8s_scale_deployment", snap, "trace-scale")

        assert ok is True
        assert "rollback_ok" in msg
        assert "replicas=3" in msg

    @pytest.mark.asyncio
    async def test_configmap_rollback_no_prior_value_skips(self):
        redis = AsyncMock()
        snap = {
            "tool_name": "k8s_patch_configmap",
            "namespace": "ns",
            "key": "MY_KEY",
            "prior_value": None,
        }
        redis.get.return_value = json.dumps(snap).encode()
        ctx = self._make_ctx(redis, rollback_target_name="my-cm")

        with patch("workers.k8s_tools._load_k8s_config", new=AsyncMock()):
            from workers.rollback_executor import _apply
            ok, msg = await _apply(ctx, "k8s_patch_configmap", snap, "trace-cm-noprior")

        assert ok is False
        assert "prior_value was None" in msg

    @pytest.mark.asyncio
    async def test_create_or_patch_rollback_deletes_new_configmap(self):
        redis = AsyncMock()
        snap = {
            "tool_name": "k8s_create_or_patch_configmap",
            "namespace": "ns",
            "key": "KEY",
            "configmap_existed": False,  # ConfigMap was created by the mutate
        }
        redis.get.return_value = json.dumps(snap).encode()
        ctx = self._make_ctx(redis, rollback_target_name="new-cm")

        mock_v1 = AsyncMock()
        mock_v1.delete_namespaced_config_map.return_value = None
        mock_v1.api_client.close = AsyncMock()

        mock_client = MagicMock()
        mock_client.CoreV1Api.return_value = mock_v1

        with (
            patch("workers.k8s_tools._load_k8s_config", new=AsyncMock()),
            patch("workers.rollback_executor.client", mock_client),
        ):
            from workers.rollback_executor import _apply
            ok, msg = await _apply(ctx, "k8s_create_or_patch_configmap", snap, "trace-create-cm")

        assert ok is True
        assert "deleted created configmap" in msg
        mock_v1.delete_namespaced_config_map.assert_called_once_with("new-cm", "ns")
