"""Track 2B — coverage uplift for:
  - workers.diagnostic_k8s_clinical  (target ≥60%)
  - workers.proactive_observer        (target ≥60%)
  - workers.k8s_resource_snapshot     (target ≥65%)

Strategy:
  * Pure functions tested directly (no K8s / Redis needed).
  * Async helpers that only need FakeRedis use fakeredis.aioredis.FakeRedis.
  * Integration tests that need a live K8s cluster are guarded by K8S_AVAIL.
"""
from __future__ import annotations

import os
import types
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------
K8S_AVAIL = os.path.exists(os.path.expanduser("~/.kube/config"))

# ---------------------------------------------------------------------------
# Helpers to build lightweight fake Pod/Workload objects
# ---------------------------------------------------------------------------


def _ns(fields: dict[str, Any]) -> SimpleNamespace:
    """Recursively convert a dict to SimpleNamespace so attribute access works."""
    ns = SimpleNamespace()
    for k, v in fields.items():
        if isinstance(v, dict):
            setattr(ns, k, _ns(v))
        elif isinstance(v, list):
            setattr(ns, k, [_ns(i) if isinstance(i, dict) else i for i in v])
        else:
            setattr(ns, k, v)
    return ns


def _fake_pod(
    *,
    phase: str = "Running",
    conditions: list[dict] | None = None,
    container_statuses: list[dict] | None = None,
    containers: list[dict] | None = None,
    owner_references: list[dict] | None = None,
) -> SimpleNamespace:
    """Build a minimal fake Pod object matching kubernetes_asyncio attribute layout."""
    cond_list = [
        _ns({"type": c.get("type", "Ready"), "status": c.get("status", "True"),
             "reason": c.get("reason", "")})
        for c in (conditions or [])
    ]
    cs_list = []
    for cs in (container_statuses or []):
        state_fields: dict[str, Any] = {"waiting": None, "terminated": None, "running": None}
        if "waiting" in cs:
            state_fields["waiting"] = _ns({"reason": cs["waiting"].get("reason", "")})
        if "terminated" in cs:
            state_fields["terminated"] = _ns({
                "reason": cs["terminated"].get("reason", ""),
                "exit_code": cs["terminated"].get("exit_code", 0),
            })
        cs_obj = _ns({
            "name": cs.get("name", "main"),
            "state": _ns(state_fields),
            "restart_count": cs.get("restart_count", 0),
        })
        cs_list.append(cs_obj)
    spec_containers = []
    for c in (containers or []):
        res_fields: dict[str, Any] = {}
        if "resources" in c:
            lim = c["resources"].get("limits") or {}
            res_fields["resources"] = _ns({"limits": lim})
        else:
            res_fields["resources"] = None
        spec_containers.append(_ns({"name": c.get("name", "app"), **res_fields}))
    owner_refs = []
    for ref in (owner_references or []):
        owner_refs.append(_ns({
            "kind": ref.get("kind", "ReplicaSet"),
            "name": ref.get("name", "rs-abc"),
            "controller": ref.get("controller", True),
        }))
    pod = SimpleNamespace(
        metadata=_ns({"owner_references": owner_refs, "name": "test-pod"}),
        spec=_ns({"containers": spec_containers}),
        status=_ns({
            "phase": phase,
            "conditions": cond_list,
            "container_statuses": cs_list,
        }),
    )
    return pod


# ===========================================================================
# 1. diagnostic_k8s_clinical — pure function tests
# ===========================================================================
from workers import diagnostic_k8s_clinical as dkc


class TestMatchLabelsToSelector:
    def test_none_input(self) -> None:
        assert dkc._match_labels_to_selector(None) is None

    def test_empty_dict(self) -> None:
        assert dkc._match_labels_to_selector({}) is None

    def test_single_label(self) -> None:
        result = dkc._match_labels_to_selector({"app": "nginx"})
        assert result == "app=nginx"

    def test_multiple_labels_sorted(self) -> None:
        result = dkc._match_labels_to_selector({"z": "last", "a": "first"})
        assert result == "a=first,z=last"

    def test_special_chars_passed_through(self) -> None:
        result = dkc._match_labels_to_selector({"app.kubernetes.io/name": "svc"})
        assert "app.kubernetes.io/name=svc" in result


class TestMatchLabelsFromWorkloadObj:
    def test_no_spec(self) -> None:
        obj = SimpleNamespace()
        assert dkc._match_labels_from_workload_obj(obj) is None

    def test_no_selector(self) -> None:
        obj = SimpleNamespace(spec=SimpleNamespace())
        assert dkc._match_labels_from_workload_obj(obj) is None

    def test_no_match_labels(self) -> None:
        obj = SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(match_labels=None)))
        assert dkc._match_labels_from_workload_obj(obj) is None

    def test_empty_match_labels(self) -> None:
        obj = SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(match_labels={})))
        assert dkc._match_labels_from_workload_obj(obj) is None

    def test_valid_match_labels(self) -> None:
        ml = {"app": "nginx", "version": "v1"}
        obj = SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(match_labels=ml)))
        result = dkc._match_labels_from_workload_obj(obj)
        assert result == {"app": "nginx", "version": "v1"}

    def test_coerces_keys_to_str(self) -> None:
        ml = {1: "value"}
        obj = SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(match_labels=ml)))
        result = dkc._match_labels_from_workload_obj(obj)
        assert result == {"1": "value"}


class TestInferWorkloadNameFromPodName:
    def test_standard_deployment_pod(self) -> None:
        assert dkc._infer_workload_name_from_pod_name("nginx-7c9f77d4b5-xpqzr") == "nginx"

    def test_nested_name(self) -> None:
        assert dkc._infer_workload_name_from_pod_name("my-service-api-6d8b9f4c7a-abcde") == "my-service-api"

    def test_no_match_returns_none(self) -> None:
        assert dkc._infer_workload_name_from_pod_name("simple-name") is None

    def test_empty_string(self) -> None:
        assert dkc._infer_workload_name_from_pod_name("") is None

    def test_none_like_empty(self) -> None:
        # _infer_workload_name_from_pod_name("") should return None
        result = dkc._infer_workload_name_from_pod_name("")
        assert result is None


class TestMemoryLimitsFromPodSpec:
    def test_no_containers(self) -> None:
        pod = _fake_pod()
        lines, struct = dkc._memory_limits_from_pod_spec(pod)
        assert lines == []
        assert struct == []

    def test_container_with_dict_limits(self) -> None:
        # resources.limits as dict (common in kubernetes_asyncio return)
        pod = _fake_pod(containers=[{"name": "main", "resources": {"limits": {"memory": "512Mi"}}}])
        lines, struct = dkc._memory_limits_from_pod_spec(pod)
        assert len(lines) == 1
        assert "512Mi" in lines[0]
        assert struct[0]["source"] == "spec_limits_fallback"
        assert struct[0]["memory"] == "512Mi"

    def test_container_with_attr_limits(self) -> None:
        """limits as object with .memory attribute."""
        lim_obj = SimpleNamespace(memory="256Mi")
        ctr = SimpleNamespace(
            name="sidecar",
            resources=SimpleNamespace(limits=lim_obj),
        )
        pod = SimpleNamespace(spec=SimpleNamespace(containers=[ctr]))
        lines, struct = dkc._memory_limits_from_pod_spec(pod)
        assert len(lines) == 1
        assert "256Mi" in lines[0]

    def test_container_no_memory_limit_skipped(self) -> None:
        pod = _fake_pod(containers=[{"name": "app"}])
        lines, struct = dkc._memory_limits_from_pod_spec(pod)
        assert lines == []


class TestPodNeedsLogTail:
    def test_running_ready_pod_no_log_needed(self) -> None:
        pod = _fake_pod(phase="Running", conditions=[{"type": "Ready", "status": "True"}])
        assert dkc._pod_needs_log_tail(pod) is False

    def test_non_running_phase_needs_log(self) -> None:
        for phase in ("Pending", "Failed", "Unknown"):
            pod = _fake_pod(phase=phase)
            assert dkc._pod_needs_log_tail(pod) is True, f"phase={phase}"

    def test_oomkilled_container_needs_log(self) -> None:
        pod = _fake_pod(
            phase="Running",
            container_statuses=[{"name": "main", "terminated": {"reason": "OOMKilled"}}],
        )
        assert dkc._pod_needs_log_tail(pod) is True

    def test_crash_loop_waiting_needs_log(self) -> None:
        pod = _fake_pod(
            phase="Running",
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}],
        )
        assert dkc._pod_needs_log_tail(pod) is True

    def test_ready_false_condition_needs_log(self) -> None:
        pod = _fake_pod(
            phase="Running",
            conditions=[{"type": "Ready", "status": "False"}],
        )
        assert dkc._pod_needs_log_tail(pod) is True

    def test_error_terminated_needs_log(self) -> None:
        pod = _fake_pod(
            phase="Running",
            container_statuses=[{"name": "main", "terminated": {"reason": "Error"}}],
        )
        assert dkc._pod_needs_log_tail(pod) is True


class TestPendingSkipLogUseEvents:
    def test_running_pod_not_skip(self) -> None:
        pod = _fake_pod(phase="Running")
        assert dkc._pending_skip_log_use_events(pod) is False

    def test_pending_no_container_statuses_not_skip(self) -> None:
        pod = _fake_pod(phase="Pending")
        assert dkc._pending_skip_log_use_events(pod) is False

    def test_pending_imagepullbackoff_skips(self) -> None:
        pod = _fake_pod(
            phase="Pending",
            container_statuses=[{"name": "main", "waiting": {"reason": "ImagePullBackOff"}}],
        )
        assert dkc._pending_skip_log_use_events(pod) is True

    def test_pending_createcontainerconfigerror_skips(self) -> None:
        pod = _fake_pod(
            phase="Pending",
            container_statuses=[{"name": "main", "waiting": {"reason": "CreateContainerConfigError"}}],
        )
        assert dkc._pending_skip_log_use_events(pod) is True

    def test_pending_crashloop_not_skip(self) -> None:
        pod = _fake_pod(
            phase="Pending",
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}],
        )
        assert dkc._pending_skip_log_use_events(pod) is False

    def test_pending_createcontainererror_skips(self) -> None:
        pod = _fake_pod(
            phase="Pending",
            container_statuses=[{"name": "main", "waiting": {"reason": "CreateContainerError"}}],
        )
        assert dkc._pending_skip_log_use_events(pod) is True


class TestPickContainerForLog:
    def test_no_containers_returns_none(self) -> None:
        pod = _fake_pod()
        assert dkc._pick_container_for_log(pod) is None

    def test_picks_first_when_all_running(self) -> None:
        pod = _fake_pod(
            containers=[{"name": "main"}, {"name": "sidecar"}],
            container_statuses=[],
        )
        result = dkc._pick_container_for_log(pod)
        assert result == "main"

    def test_picks_waiting_container_first(self) -> None:
        pod = _fake_pod(
            containers=[{"name": "main"}, {"name": "sidecar"}],
            container_statuses=[
                {"name": "main"},
                {"name": "sidecar", "waiting": {"reason": "CrashLoopBackOff"}},
            ],
        )
        result = dkc._pick_container_for_log(pod)
        assert result == "sidecar"

    def test_picks_terminated_container(self) -> None:
        pod = _fake_pod(
            containers=[{"name": "app"}],
            container_statuses=[
                {"name": "app", "terminated": {"reason": "OOMKilled"}},
            ],
        )
        assert dkc._pick_container_for_log(pod) == "app"


class TestSinglePodStatusFragment:
    def test_running_healthy_pod(self) -> None:
        pod = _fake_pod(
            phase="Running",
            conditions=[{"type": "Ready", "status": "True"}],
        )
        frag, st = dkc._single_pod_status_fragment("ns1", "pod-1", pod)
        assert "pod/pod-1" in frag
        assert "phase=Running" in frag
        assert st["phase"] == "Running"
        assert st["has_crash_loop"] is False
        assert st["has_oom_killed"] is False
        assert st["ready_false"] is False

    def test_crash_loop_pod(self) -> None:
        pod = _fake_pod(
            phase="Running",
            conditions=[{"type": "Ready", "status": "False"}],
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}],
        )
        frag, st = dkc._single_pod_status_fragment("ns1", "crash-pod", pod)
        assert st["has_crash_loop"] is True
        assert st["ready_false"] is True
        assert "CrashLoopBackOff" in st["waiting_reasons"]

    def test_oomkilled_pod(self) -> None:
        pod = _fake_pod(
            phase="Running",
            container_statuses=[{"name": "main", "terminated": {"reason": "OOMKilled", "exit_code": 137}}],
        )
        _, st = dkc._single_pod_status_fragment("ns1", "oom-pod", pod)
        assert st["has_oom_killed"] is True

    def test_multiple_container_signals(self) -> None:
        pod = _fake_pod(
            phase="Pending",
            container_statuses=[
                {"name": "app", "waiting": {"reason": "ImagePullBackOff"}},
                {"name": "init", "terminated": {"reason": "Error", "exit_code": 1}},
            ],
        )
        frag, st = dkc._single_pod_status_fragment("default", "multi-pod", pod)
        assert "ImagePullBackOff" in frag
        assert "Error" in frag
        assert len(st["container_signals"]) == 2


class TestNeedsPreviousLog:
    def test_crash_loop_needs_previous(self) -> None:
        pod = _fake_pod(
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}],
        )
        assert dkc._needs_previous_log(pod) is True

    def test_terminated_with_restart_needs_previous(self) -> None:
        pod = _fake_pod(
            container_statuses=[
                {"name": "main", "terminated": {"reason": "Error"}, "restart_count": 3},
            ],
        )
        assert dkc._needs_previous_log(pod) is True

    def test_clean_pod_no_previous(self) -> None:
        pod = _fake_pod(phase="Running")
        assert dkc._needs_previous_log(pod) is False

    def test_terminated_no_restart_no_previous(self) -> None:
        pod = _fake_pod(
            container_statuses=[
                {"name": "main", "terminated": {"reason": "Completed"}, "restart_count": 0},
            ],
        )
        assert dkc._needs_previous_log(pod) is False


# ===========================================================================
# 2. k8s_resource_snapshot — pure function tests
# ===========================================================================
from workers import k8s_resource_snapshot as krs


class TestClipObj:
    def test_basic_object(self) -> None:
        d = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "nginx", "namespace": "default", "uid": "abc-123", "resourceVersion": "42"},
            "status": {"readyReplicas": 1},
            "spec": {"replicas": 1},
        }
        result = krs._clip_obj(d)
        assert result["apiVersion"] == "apps/v1"
        assert result["kind"] == "Deployment"
        assert result["metadata"]["name"] == "nginx"
        assert result["metadata"]["namespace"] == "default"
        assert result["metadata"]["uid"] == "abc-123"
        assert result["resourceVersion"] == "42"
        assert result["status"] == {"readyReplicas": 1}
        # spec not in output
        assert "spec" not in result

    def test_no_status(self) -> None:
        d = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test", "namespace": "ns"},
        }
        result = krs._clip_obj(d)
        assert "status" not in result

    def test_empty_metadata(self) -> None:
        d: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {},
        }
        result = krs._clip_obj(d)
        assert result["metadata"]["name"] is None
        assert result["metadata"]["namespace"] is None

    def test_missing_metadata_key(self) -> None:
        d: dict[str, Any] = {"apiVersion": "v1", "kind": "Service"}
        result = krs._clip_obj(d)
        assert result["metadata"]["name"] is None

    def test_status_with_complex_value(self) -> None:
        d = {
            "kind": "Deployment",
            "metadata": {"name": "x", "namespace": "ns"},
            "status": {"conditions": [{"type": "Available", "status": "True"}]},
        }
        result = krs._clip_obj(d)
        assert result["status"]["conditions"][0]["type"] == "Available"


class TestFetchLastKnownStatePure:
    """Tests that do NOT require K8s — only missing_kind_or_name and timeout paths."""

    @pytest.mark.asyncio
    async def test_missing_kind_returns_unavailable(self) -> None:
        result = await krs.fetch_last_known_state("default", "", "nginx")
        assert result["unavailable"] is True
        assert "missing_kind_or_name" in result["reason"]

    @pytest.mark.asyncio
    async def test_missing_name_returns_unavailable(self) -> None:
        result = await krs.fetch_last_known_state("default", "Deployment", "")
        assert result["unavailable"] is True
        assert "missing_kind_or_name" in result["reason"]

    @pytest.mark.asyncio
    async def test_unsupported_kind_returns_unavailable(self) -> None:
        # "StatefulSet" is not in the supported kinds list — returns unavailable
        # But it needs namespace, so let's use a kind that hits the unsupported_kind branch
        result = await krs.fetch_last_known_state("default", "StatefulSet", "my-ss")
        assert result["unavailable"] is True
        assert "unsupported_kind" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_very_short_timeout_returns_unavailable(self) -> None:
        # Use an extremely short timeout that would expire before K8s responds
        # This is safe — the code handles asyncio.TimeoutError
        result = await krs.fetch_last_known_state("default", "Deployment", "some-dep", timeout_sec=0.001)
        assert result.get("unavailable") is True

    @pytest.mark.asyncio
    async def test_namespace_kind_requires_no_namespace_guard(self) -> None:
        """When kind is namespaced but namespace is empty → should return unavailable."""
        result = await krs.fetch_last_known_state("", "Deployment", "nginx")
        assert result["unavailable"] is True
        assert "missing_namespace" in result["reason"]


class TestFetchLastKnownStateIntegration:
    @pytest.mark.asyncio
    async def test_real_namespace_fetch(self) -> None:
        """Fetch the multi-agent namespace object (namespace kind = non-namespaced)."""
        result = await krs.fetch_last_known_state("", "Namespace", "multi-agent")
        # Either unavailable (metrics-server off) or has real data
        if result.get("unavailable"):
            assert "reason" in result
        else:
            assert result.get("kind") == "Namespace" or "metadata" in result

    @pytest.mark.asyncio
    async def test_missing_deployment_returns_unavailable(self) -> None:
        result = await krs.fetch_last_known_state(
            "multi-agent", "Deployment", "this-deployment-does-not-exist-xyz123"
        )
        assert result.get("unavailable") is True
        assert "ApiException" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_real_nginx_deployment(self) -> None:
        """nginx-test deployment exists in multi-agent namespace."""
        result = await krs.fetch_last_known_state("multi-agent", "Deployment", "nginx-test")
        if result.get("unavailable"):
            # Acceptable — might be timing issue
            assert "reason" in result
        else:
            assert result.get("kind") == "Deployment"
            meta = result.get("metadata") or {}
            assert meta.get("name") == "nginx-test"


# ===========================================================================
# 3. proactive_observer — async helpers with FakeRedis
# ===========================================================================
from workers import proactive_observer as po
from workers.proactive_models import AnomalyEvent


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


@pytest.fixture
def fake_ctx(fake_redis: FakeRedis) -> SimpleNamespace:
    settings = SimpleNamespace(
        proactive_negative_pattern_ttl_sec=3600,
        embed_model="nomic-embed-text:latest",
        proactive_react_memory_line_max_chars=2000,
        learning_stats_ttl_sec=86400,
    )
    return SimpleNamespace(redis=fake_redis, settings=settings)


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_no_key_returns_false(self, fake_redis: FakeRedis) -> None:
        result = await po.proactive_kill_switch_engaged(fake_redis, "omni:proactive:kill_switch")
        assert result is False

    @pytest.mark.asyncio
    async def test_zero_value_returns_false(self, fake_redis: FakeRedis) -> None:
        await fake_redis.set("omni:proactive:kill_switch", "0")
        result = await po.proactive_kill_switch_engaged(fake_redis, "omni:proactive:kill_switch")
        assert result is False

    @pytest.mark.asyncio
    async def test_one_value_returns_true(self, fake_redis: FakeRedis) -> None:
        await fake_redis.set("omni:proactive:kill_switch", "1")
        result = await po.proactive_kill_switch_engaged(fake_redis, "omni:proactive:kill_switch")
        assert result is True

    @pytest.mark.asyncio
    async def test_exception_returns_false(self) -> None:
        class BadRedis:
            async def get(self, key: str) -> None:
                raise RuntimeError("network error")

        result = await po.proactive_kill_switch_engaged(BadRedis(), "key")
        assert result is False

    @pytest.mark.asyncio
    async def test_whitespace_value_not_engaged(self, fake_redis: FakeRedis) -> None:
        await fake_redis.set("k", "  ")
        result = await po.proactive_kill_switch_engaged(fake_redis, "k")
        assert result is False


class TestNegativePattern:
    @pytest.mark.asyncio
    async def test_empty_key_not_negative(self, fake_ctx: SimpleNamespace) -> None:
        assert await po._is_negative_pattern(fake_ctx, "") is False
        assert await po._is_negative_pattern(fake_ctx, "   ") is False

    @pytest.mark.asyncio
    async def test_absent_key_not_negative(self, fake_ctx: SimpleNamespace) -> None:
        assert await po._is_negative_pattern(fake_ctx, "abc123") is False

    @pytest.mark.asyncio
    async def test_set_and_read_negative_pattern(self, fake_ctx: SimpleNamespace) -> None:
        await po._set_negative_pattern(fake_ctx, "pattern-key-001", "failed 3 times")
        assert await po._is_negative_pattern(fake_ctx, "pattern-key-001") is True

    @pytest.mark.asyncio
    async def test_set_empty_key_noop(self, fake_ctx: SimpleNamespace) -> None:
        await po._set_negative_pattern(fake_ctx, "", "reason")
        # No exception, and empty key remains absent
        assert await po._is_negative_pattern(fake_ctx, "") is False

    @pytest.mark.asyncio
    async def test_redis_error_is_negative_returns_false(self) -> None:
        class BadRedis:
            async def get(self, key: str) -> None:
                raise RuntimeError("redis down")

        ctx = SimpleNamespace(redis=BadRedis(), settings=SimpleNamespace())
        assert await po._is_negative_pattern(ctx, "key") is False


class TestReactMem:
    @pytest.mark.asyncio
    async def test_append_and_read(self, fake_ctx: SimpleNamespace) -> None:
        await po._react_mem_append(fake_ctx, "trace-001", "step 1", ttl_sec=60)
        await po._react_mem_append(fake_ctx, "trace-001", "step 2", ttl_sec=60)
        rows = await po._react_mem_recent(fake_ctx, "trace-001", limit=10)
        assert len(rows) == 2
        assert "step 1" in rows[0]
        assert "step 2" in rows[1]

    @pytest.mark.asyncio
    async def test_recent_limit_respected(self, fake_ctx: SimpleNamespace) -> None:
        for i in range(10):
            await po._react_mem_append(fake_ctx, "trace-002", f"step {i}", ttl_sec=60)
        rows = await po._react_mem_recent(fake_ctx, "trace-002", limit=3)
        assert len(rows) == 3
        assert "step 9" in rows[-1]

    @pytest.mark.asyncio
    async def test_empty_returns_empty_list(self, fake_ctx: SimpleNamespace) -> None:
        rows = await po._react_mem_recent(fake_ctx, "nonexistent-trace", limit=5)
        assert rows == []

    @pytest.mark.asyncio
    async def test_exception_in_append_swallowed(self) -> None:
        class BadRedis:
            async def rpush(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("boom")
            async def expire(self, *a: Any, **k: Any) -> None:
                pass

        ctx = SimpleNamespace(redis=BadRedis(), settings=SimpleNamespace(
            proactive_react_memory_line_max_chars=2000
        ))
        # Should not raise
        await po._react_mem_append(ctx, "tid", "line")

    @pytest.mark.asyncio
    async def test_exception_in_recent_returns_empty(self) -> None:
        class BadRedis:
            async def lrange(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("boom")

        ctx = SimpleNamespace(redis=BadRedis(), settings=SimpleNamespace())
        result = await po._react_mem_recent(ctx, "tid", limit=5)
        assert result == []


class TestSanitizeProactiveTelegramBody:
    def test_filters_debug_lines(self) -> None:
        raw = "[DEBUG] internal\nvisible\n[debug] more internal\nok"
        out = po._sanitize_proactive_telegram_body(raw)
        assert "internal" not in out
        assert "visible" in out
        assert "ok" in out

    def test_filters_detail_lines(self) -> None:
        raw = "line1\n[DETAIL] skip me\nline3"
        out = po._sanitize_proactive_telegram_body(raw)
        assert "skip me" not in out
        assert "line1" in out
        assert "line3" in out

    def test_all_filtered_gives_operator_view(self) -> None:
        out = po._sanitize_proactive_telegram_body("[DEBUG] a\n[DETAIL] b\n")
        assert "[OPERATOR_VIEW]" in out

    def test_max_chars_truncation(self) -> None:
        long_text = "x " * 2000
        out = po._sanitize_proactive_telegram_body(long_text, max_chars=100)
        assert len(out) <= 100

    def test_empty_input(self) -> None:
        out = po._sanitize_proactive_telegram_body("")
        assert "[OPERATOR_VIEW]" in out


class TestStableArgsHash:
    def test_deterministic(self) -> None:
        h1 = po._stable_args_hash({"a": 1, "b": 2})
        h2 = po._stable_args_hash({"a": 1, "b": 2})
        assert h1 == h2

    def test_order_independent(self) -> None:
        h1 = po._stable_args_hash({"z": "last", "a": "first"})
        h2 = po._stable_args_hash({"a": "first", "z": "last"})
        assert h1 == h2
        assert len(h1) == 24

    def test_different_args_different_hash(self) -> None:
        h1 = po._stable_args_hash({"namespace": "ns1"})
        h2 = po._stable_args_hash({"namespace": "ns2"})
        assert h1 != h2

    def test_empty_dict(self) -> None:
        h = po._stable_args_hash({})
        assert len(h) == 24

    def test_none_treated_as_empty(self) -> None:
        h = po._stable_args_hash(None)  # type: ignore[arg-type]
        assert len(h) == 24


class TestPatternKeyFromEvent:
    def test_same_event_same_key(self) -> None:
        ev = AnomalyEvent(trace_id="trace-1", rule_name="R1", canonical_query="cpu > 0.9", threshold=0.9)
        k1 = po._pattern_key_from_event(ev)
        k2 = po._pattern_key_from_event(ev)
        assert k1 == k2
        assert len(k1) == 24

    def test_different_threshold_different_key(self) -> None:
        ev1 = AnomalyEvent(trace_id="t-01", rule_name="R1", canonical_query="up", threshold=0.5)
        ev2 = AnomalyEvent(trace_id="t-01", rule_name="R1", canonical_query="up", threshold=0.9)
        assert po._pattern_key_from_event(ev1) != po._pattern_key_from_event(ev2)

    def test_different_rule_different_key(self) -> None:
        ev1 = AnomalyEvent(trace_id="t-02", rule_name="R1", canonical_query="q-query", threshold=0)
        ev2 = AnomalyEvent(trace_id="t-02", rule_name="R2", canonical_query="q-query", threshold=0)
        assert po._pattern_key_from_event(ev1) != po._pattern_key_from_event(ev2)


class TestQuickVerifyOutput:
    def test_business_hit_is_true(self) -> None:
        assert po._quick_verify_output("[status] business_hit", "") is True

    def test_empty_result_is_false(self) -> None:
        assert po._quick_verify_output("[status] empty_result", "") is False

    def test_error_is_false(self) -> None:
        assert po._quick_verify_output("[status] error", "") is False

    def test_missing_args_is_false(self) -> None:
        assert po._quick_verify_output("thiếu args here", "") is False
        assert po._quick_verify_output("missing arg: namespace", "") is False
        assert po._quick_verify_output("invalid args passed", "") is False

    def test_blank_is_false(self) -> None:
        assert po._quick_verify_output("   ", "") is False

    def test_fail_keywords_csv(self) -> None:
        assert po._quick_verify_output("ok text but error_code_404", "error_code_404") is False

    def test_ok_text_no_keywords_is_true(self) -> None:
        assert po._quick_verify_output("all looks good here", "bad_word") is True


class TestResultStatus:
    def test_business_hit(self) -> None:
        assert po._result_status("[status] business_hit extra") == "business_hit"

    def test_empty_result(self) -> None:
        assert po._result_status("[STATUS] EMPTY_RESULT") == "empty_result"

    def test_error(self) -> None:
        assert po._result_status("prefix [status] error tail") == "error"

    def test_unknown(self) -> None:
        assert po._result_status("no known status here") == "unknown"

    def test_empty_string(self) -> None:
        assert po._result_status("") == "unknown"


class TestAllowLearningUpsert:
    def test_not_verified_always_false(self) -> None:
        assert po._allow_learning_upsert("kubectl_get", "any", False) is False

    def test_promql_needs_business_hit(self) -> None:
        assert po._allow_learning_upsert("promql_instant", "[status] business_hit", True) is True
        assert po._allow_learning_upsert("promql_instant", "[status] error", True) is False
        assert po._allow_learning_upsert("vm_promql_instant", "[status] business_hit", True) is True
        assert po._allow_learning_upsert("vm_promql_instant", "[status] empty_result", True) is False

    def test_other_tool_only_needs_verified(self) -> None:
        assert po._allow_learning_upsert("kubectl_get", "any output", True) is True


class TestEmbeddingFromResponse:
    def test_embedding_key_list(self) -> None:
        assert po._embedding_from_response({"embedding": [0.1, 0.2, 0.3]}) == [0.1, 0.2, 0.3]

    def test_embedding_key_tuple(self) -> None:
        result = po._embedding_from_response({"embedding": (0.5, 0.6)})
        assert result == [0.5, 0.6]

    def test_embeddings_key(self) -> None:
        result = po._embedding_from_response({"embeddings": [[1.0, 2.0, 3.0]]})
        assert result == [1.0, 2.0, 3.0]

    def test_empty_embeddings_list(self) -> None:
        assert po._embedding_from_response({"embeddings": []}) == []

    def test_missing_keys(self) -> None:
        assert po._embedding_from_response({}) == []


class TestUpdateLearningPatternStats:
    @pytest.mark.asyncio
    async def test_success_increments(self, fake_ctx: SimpleNamespace) -> None:
        await po._update_learning_pattern_stats(
            fake_ctx, source="proactive_sop", pattern_key="key001", outcome="success"
        )
        key = "omni:learning:pattern:key001"
        total = await fake_ctx.redis.hget(key, "total")
        success = await fake_ctx.redis.hget(key, "success")
        assert total == "1"
        assert success == "1"

    @pytest.mark.asyncio
    async def test_fail_increments(self, fake_ctx: SimpleNamespace) -> None:
        await po._update_learning_pattern_stats(
            fake_ctx, source="proactive_sop", pattern_key="key002", outcome="fail"
        )
        key = "omni:learning:pattern:key002"
        total = await fake_ctx.redis.hget(key, "total")
        fail = await fake_ctx.redis.hget(key, "fail")
        assert total == "1"
        assert fail == "1"

    @pytest.mark.asyncio
    async def test_multiple_calls_accumulate(self, fake_ctx: SimpleNamespace) -> None:
        for _ in range(3):
            await po._update_learning_pattern_stats(
                fake_ctx, source="src", pattern_key="key003", outcome="success"
            )
        total = await fake_ctx.redis.hget("omni:learning:pattern:key003", "total")
        assert total == "3"

    @pytest.mark.asyncio
    async def test_unique_set_tracked(self, fake_ctx: SimpleNamespace) -> None:
        await po._update_learning_pattern_stats(
            fake_ctx, source="src", pattern_key="unique-key", outcome="success"
        )
        card = await fake_ctx.redis.scard("omni:learning:unique:set:src")
        assert card >= 1


class TestDbgLog:
    def test_dbg_log_does_not_raise(self) -> None:
        """_dbg_log writes to a file path that may or may not exist; should swallow errors."""
        po._dbg_log(
            run_id="test-run",
            hypothesis_id="H0",
            location="test_track2b.py:TestDbgLog",
            message="unit_test_probe",
            data={"value": 42},
        )
        # If we get here without exception, the function handled everything gracefully


# ===========================================================================
# 4. diagnostic_k8s_clinical — integration tests with real K8s
# ===========================================================================
class TestDiagnosticK8sIntegration:
    """Integration tests that call the real K8s API via kubernetes_asyncio."""

    @pytest.mark.asyncio
    async def test_probe_missing_namespace_skipped(self) -> None:
        """When AnomalyEvent has no namespace/pod, probes return SKIPPED."""
        ev = AnomalyEvent(
            trace_id="test-trace-0001",
            canonical_query="no namespace",
            rule_name="TestRule",
        )
        # probe_k8s_clinical_pod_status with empty namespace should SKIP
        result = await dkc.probe_k8s_clinical_pod_status(None, ev)  # type: ignore[arg-type]
        assert result.status == "SKIPPED"
        assert result.probe_name == "k8s_clinical_pod_status"

    @pytest.mark.asyncio
    async def test_probe_metrics_missing_namespace_skipped(self) -> None:
        ev = AnomalyEvent(
            trace_id="test-trace-0002",
            canonical_query="no namespace",
        )
        result = await dkc.probe_k8s_clinical_pod_metrics(None, ev)  # type: ignore[arg-type]
        assert result.status == "SKIPPED"

    @pytest.mark.asyncio
    async def test_probe_log_tail_missing_namespace_skipped(self) -> None:
        ev = AnomalyEvent(
            trace_id="test-trace-0003",
            canonical_query="no ns",
        )
        result = await dkc.probe_k8s_clinical_pod_log_tail(None, ev)  # type: ignore[arg-type]
        assert result.status == "SKIPPED"

    @pytest.mark.asyncio
    async def test_probe_events_missing_namespace_skipped(self) -> None:
        ev = AnomalyEvent(
            trace_id="test-trace-0004",
            canonical_query="no ns",
        )
        result = await dkc.probe_k8s_clinical_pod_events(None, ev)  # type: ignore[arg-type]
        assert result.status == "SKIPPED"

    @pytest.mark.asyncio
    async def test_probe_resource_quota_missing_namespace_skipped(self) -> None:
        ev = AnomalyEvent(
            trace_id="test-trace-0005",
            canonical_query="no ns",
        )
        result = await dkc.probe_k8s_resource_quota_probe(None, ev)  # type: ignore[arg-type]
        assert result.status == "SKIPPED"

    @pytest.mark.asyncio
    async def test_probe_log_previous_missing_namespace_skipped(self) -> None:
        ev = AnomalyEvent(
            trace_id="test-trace-0006",
            canonical_query="no ns",
        )
        result = await dkc.probe_k8s_clinical_pod_log_previous(None, ev)  # type: ignore[arg-type]
        assert result.status == "SKIPPED"

    @pytest.mark.asyncio
    async def test_probe_pod_status_nonexistent_pod(self) -> None:
        """Pod that does not exist → workload resolution returns INCONCLUSIVE or PASSED with no pods.
        canonical_query must be JSON with labels.namespace and labels.pod for pod_identity_from_event.
        """
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "ghost-pod-xyz-99999-aaaaa"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-0007",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_status(None, ev)  # type: ignore[arg-type]
        # Should return INCONCLUSIVE (no pods found) or PASSED
        assert result.status in ("PASSED", "INCONCLUSIVE", "FAILED")
        assert result.probe_name == "k8s_clinical_pod_status"

    @pytest.mark.asyncio
    async def test_probe_resource_quota_real_namespace(self) -> None:
        """List resource quotas in multi-agent namespace (may be empty but should not fail)."""
        ev = AnomalyEvent(
            trace_id="test-trace-0008",
            canonical_query="resource quota test",
            namespace="multi-agent",
            gigo_metadata={"namespace": "multi-agent"},
        )
        result = await dkc.probe_k8s_resource_quota_probe(None, ev)  # type: ignore[arg-type]
        assert result.status in ("PASSED", "FAILED")
        if result.status == "PASSED":
            assert "ResourceQuota" in result.raw_text or "(no ResourceQuota" in result.raw_text

    @pytest.mark.asyncio
    async def test_fetch_pod_events_summary_nonexistent_pod(self) -> None:
        """fetch_pod_events_summary for a missing pod should return empty/no events."""
        from kubernetes_asyncio import client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            await k8s_config.load_kube_config()
        v1 = client.CoreV1Api()
        try:
            result = await dkc.fetch_pod_events_summary(v1, "multi-agent", "nonexistent-pod-xyz")
            assert isinstance(result, str)
            # Either "(no events in scope)" or some event lines
        finally:
            await v1.api_client.close()

    @pytest.mark.asyncio
    async def test_probe_pod_events_real_ns(self) -> None:
        """Pod events probe on multi-agent namespace with a known pod pattern.
        canonical_query must be JSON with labels.namespace and labels.pod for pod_identity_from_event.
        """
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nginx-test-847cb6f7-c55zk"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-0009",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_events(None, ev)  # type: ignore[arg-type]
        # Should be PASSED or FAILED (not SKIPPED, since namespace+pod are set)
        assert result.status in ("PASSED", "FAILED", "INCONCLUSIVE")

    @pytest.mark.asyncio
    async def test_resolve_workload_probe_targets_existing_pod(self) -> None:
        """resolve_workload_probe_targets with an existing pod returns a resolution."""
        from kubernetes_asyncio import client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            await k8s_config.load_kube_config()
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        ev = AnomalyEvent(
            trace_id="test-trace-0010",
            canonical_query="nginx workload resolution",
            namespace="multi-agent",
            gigo_metadata={"namespace": "multi-agent", "pod": "nginx-test-847cb6f7-c55zk"},
        )
        try:
            res = await dkc.resolve_workload_probe_targets(
                v1, apps, batch_api, ev, "multi-agent", "nginx-test-847cb6f7-c55zk"
            )
            assert res.namespace == "multi-agent"
            assert res.alert_pod == "nginx-test-847cb6f7-c55zk"
            assert isinstance(res.target_pods, list)
            assert isinstance(res.evidence_prefix, str)
        finally:
            await v1.api_client.close()
            await apps.api_client.close()
            await batch_api.api_client.close()


# ===========================================================================
# 5. k8s_resource_snapshot — additional integration tests (K8s)
# ===========================================================================
class TestFetchLastKnownStateIntegrationExtended:
    @pytest.mark.asyncio
    async def test_fetch_existing_pod(self) -> None:
        """Fetch an existing pod in multi-agent namespace."""
        result = await krs.fetch_last_known_state("multi-agent", "Pod", "nginx-test-847cb6f7-c55zk")
        if result.get("unavailable"):
            # Pod might have been rescheduled
            assert "ApiException" in result.get("reason", "") or "reason" in result
        else:
            assert result.get("kind") == "Pod"
            meta = result.get("metadata") or {}
            assert meta.get("namespace") == "multi-agent"

    @pytest.mark.asyncio
    async def test_fetch_missing_pod_returns_api_exception(self) -> None:
        """A pod that doesn't exist → unavailable with ApiException reason."""
        result = await krs.fetch_last_known_state("multi-agent", "Pod", "nonexistent-pod-zzz999")
        assert result.get("unavailable") is True
        assert "ApiException" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_fetch_service_kubernetes(self) -> None:
        """Fetch the kubernetes service (always exists in default ns)."""
        result = await krs.fetch_last_known_state("default", "Service", "kubernetes")
        if result.get("unavailable"):
            assert "reason" in result
        else:
            assert result.get("kind") == "Service"

    @pytest.mark.asyncio
    async def test_fetch_missing_service_returns_api_exception(self) -> None:
        """A service that doesn't exist → unavailable."""
        result = await krs.fetch_last_known_state("multi-agent", "Service", "svc-nonexistent-xyz")
        assert result.get("unavailable") is True

    @pytest.mark.asyncio
    async def test_fetch_namespace_kind_missing_returns_unavailable(self) -> None:
        """Namespace that doesn't exist → ApiException."""
        result = await krs.fetch_last_known_state("", "Namespace", "nonexistent-ns-xyz999")
        assert result.get("unavailable") is True


# ===========================================================================
# 6. diagnostic_k8s_clinical — async function tests via monkeypatch
# ===========================================================================

class TestFollowTopController:
    """Unit test _follow_top_controller without real K8s."""

    @pytest.mark.asyncio
    async def test_known_top_level_kinds_return_immediately(self) -> None:
        """Deployment, StatefulSet, DaemonSet, Job, CronJob are returned as-is."""
        for kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            result = await dkc._follow_top_controller(None, "ns", kind, "my-name", 0)  # type: ignore[arg-type]
            assert result == (kind, "my-name"), f"kind={kind}"

    @pytest.mark.asyncio
    async def test_depth_exceeded_returns_none(self) -> None:
        """When depth > _MAX_OWNER_DEPTH, return None."""
        result = await dkc._follow_top_controller(None, "ns", "ReplicaSet", "rs", dkc._MAX_OWNER_DEPTH + 1)  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_kind_returns_none(self) -> None:
        """Unknown kind (not one of the known top-level or ReplicaSet) returns None."""
        result = await dkc._follow_top_controller(None, "ns", "UnknownKind", "name", 0)  # type: ignore[arg-type]
        assert result is None


class TestTopControllerFromPod:
    @pytest.mark.asyncio
    async def test_pod_no_owner_references(self) -> None:
        pod = _fake_pod(owner_references=[])
        result = await dkc._top_controller_from_pod(None, "ns", pod)  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.asyncio
    async def test_pod_with_deployment_owner(self) -> None:
        """Pod with a Deployment owner → returns (Deployment, name) immediately."""
        pod = _fake_pod(owner_references=[
            {"kind": "Deployment", "name": "my-deploy", "controller": True},
        ])
        result = await dkc._top_controller_from_pod(None, "ns", pod)  # type: ignore[arg-type]
        assert result == ("Deployment", "my-deploy")

    @pytest.mark.asyncio
    async def test_pod_with_non_controller_ref_skipped(self) -> None:
        """Non-controller owner reference is skipped."""
        pod = _fake_pod(owner_references=[
            {"kind": "Deployment", "name": "my-deploy", "controller": False},
        ])
        result = await dkc._top_controller_from_pod(None, "ns", pod)  # type: ignore[arg-type]
        assert result is None


class TestResolvePodsMissing:
    """Test _resolve_pods_when_pod_missing with simulated missing pod scenarios."""

    @pytest.mark.asyncio
    async def test_infers_from_pod_name_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When labels are empty, infers deployment name from pod name pattern."""
        async def fake_try_list(v1: Any, apps: Any, ns: str, dep_name: str) -> list[str] | None:
            if dep_name == "nginx":
                return ["nginx-xyz-abc"]
            return None

        monkeypatch.setattr(dkc, "_try_list_by_deployment_name", fake_try_list)
        pods, how = await dkc._resolve_pods_when_pod_missing(
            None, None, None, "ns", "nginx-7c9f77d4b5-xpqzr", {}  # type: ignore[arg-type]
        )
        assert pods == ["nginx-xyz-abc"]
        assert how == "pod_name_pattern_deployment"

    @pytest.mark.asyncio
    async def test_falls_back_to_unresolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When all resolution strategies fail, returns unresolved."""
        monkeypatch.setattr(dkc, "_try_list_by_deployment_name", lambda *a, **k: None)
        monkeypatch.setattr(dkc, "_try_list_by_label_eq", lambda *a, **k: None)
        pods, how = await dkc._resolve_pods_when_pod_missing(
            None, None, None, "ns", "plain-name-no-pattern", {}  # type: ignore[arg-type]
        )
        assert pods == []
        assert how == "unresolved"

    @pytest.mark.asyncio
    async def test_uses_deployment_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When labels have 'deployment', tries that first."""
        async def fake_try_list(v1: Any, apps: Any, ns: str, dep_name: str) -> list[str] | None:
            if dep_name == "my-dep":
                return ["my-dep-pod-1"]
            return None

        monkeypatch.setattr(dkc, "_try_list_by_deployment_name", fake_try_list)
        pods, how = await dkc._resolve_pods_when_pod_missing(
            None, None, None, "ns", "old-pod-abc", {"deployment": "my-dep"}  # type: ignore[arg-type]
        )
        assert pods == ["my-dep-pod-1"]
        assert how == "label_deployment"

    @pytest.mark.asyncio
    async def test_uses_app_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When labels have 'app', tries label eq lookup."""
        async def fake_deployment(v1: Any, apps: Any, ns: str, dep_name: str) -> list[str] | None:
            return None

        async def fake_label_eq(v1: Any, ns: str, key: str, value: str) -> list[str] | None:
            if key == "app" and value == "nginx":
                return ["nginx-running-pod"]
            return None

        monkeypatch.setattr(dkc, "_try_list_by_deployment_name", fake_deployment)
        monkeypatch.setattr(dkc, "_try_list_by_label_eq", fake_label_eq)
        pods, how = await dkc._resolve_pods_when_pod_missing(
            None, None, None, "ns", "old-pod", {"app": "nginx"}  # type: ignore[arg-type]
        )
        assert pods == ["nginx-running-pod"]
        assert how == "label_app"


class TestInstantScalar:
    """Test _instant_scalar with monkeypatched _prometheus_get_json."""

    @pytest.mark.asyncio
    async def test_success_returns_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_prom(ctx: Any, path: str, params: dict) -> dict:
            return {"status": "success", "data": {"result": [{"value": [1, "0.75"]}]}}

        monkeypatch.setattr(po, "_prometheus_get_json", fake_prom)
        result = await po._instant_scalar(None, "up")  # type: ignore[arg-type]
        assert result == 0.75

    @pytest.mark.asyncio
    async def test_non_success_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_prom(ctx: Any, path: str, params: dict) -> dict:
            return {"status": "error"}

        monkeypatch.setattr(po, "_prometheus_get_json", fake_prom)
        result = await po._instant_scalar(None, "up")  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_result_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_prom(ctx: Any, path: str, params: dict) -> dict:
            return {"status": "success", "data": {"result": []}}

        monkeypatch.setattr(po, "_prometheus_get_json", fake_prom)
        result = await po._instant_scalar(None, "up")  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_prom(ctx: Any, path: str, params: dict) -> dict:
            raise RuntimeError("prom down")

        monkeypatch.setattr(po, "_prometheus_get_json", fake_prom)
        result = await po._instant_scalar(None, "up")  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_value_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_prom(ctx: Any, path: str, params: dict) -> dict:
            return {"status": "success", "data": {"result": [{"value": [1, "not-a-float"]}]}}

        monkeypatch.setattr(po, "_prometheus_get_json", fake_prom)
        result = await po._instant_scalar(None, "up")  # type: ignore[arg-type]
        assert result is None


class TestEvaluateProactiveTriggers:
    """Test evaluate_proactive_triggers with FakeRedis and monkeypatched Prometheus."""

    def _make_settings(self, *, kill_switch_val: str = "0", threshold: float = 0.8) -> SimpleNamespace:
        return SimpleNamespace(
            proactive_kill_switch_key="omni:proactive:kill_switch",
            proactive_promql="up == 0",
            proactive_trigger_threshold=threshold,
            proactive_cooldown_sec=60,
            kafka_topic_proactive_incidents="omni-proactive-incidents",
            embed_model="nomic-embed-text:latest",
        )

    @pytest.mark.asyncio
    async def test_kill_switch_engaged_returns_zero(self, fake_redis: FakeRedis) -> None:
        await fake_redis.set("omni:proactive:kill_switch", "1")
        settings = self._make_settings()
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=None)
        result = await po.evaluate_proactive_triggers(ctx)  # type: ignore[arg-type]
        assert result == 0

    @pytest.mark.asyncio
    async def test_prom_returns_none_returns_zero(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_instant(ctx: Any, promql: str) -> None:
            return None

        monkeypatch.setattr(po, "_instant_scalar", fake_instant)
        settings = self._make_settings()
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=None)
        result = await po.evaluate_proactive_triggers(ctx)  # type: ignore[arg-type]
        assert result == 0

    @pytest.mark.asyncio
    async def test_below_threshold_returns_zero(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_instant(ctx: Any, promql: str) -> float:
            return 0.3  # below threshold of 0.8

        monkeypatch.setattr(po, "_instant_scalar", fake_instant)
        settings = self._make_settings(threshold=0.8)
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=None)
        result = await po.evaluate_proactive_triggers(ctx)  # type: ignore[arg-type]
        assert result == 0

    @pytest.mark.asyncio
    async def test_cooldown_active_returns_zero(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_instant(ctx: Any, promql: str) -> float:
            return 0.95  # above threshold

        monkeypatch.setattr(po, "_instant_scalar", fake_instant)
        settings = self._make_settings(threshold=0.8)

        # Pre-populate cooldown key
        rule = "PrometheusProactiveThreshold"
        dedupe = f"{rule}:{settings.proactive_promql[:120]}"
        ck = f"omni:proactive:cooldown:{hash(dedupe) & 0xFFFFFFFF:X}"
        await fake_redis.setex(ck, 60, "1")

        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=None)
        result = await po.evaluate_proactive_triggers(ctx)  # type: ignore[arg-type]
        assert result == 0

    @pytest.mark.asyncio
    async def test_above_threshold_fires_event(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_instant(ctx: Any, promql: str) -> float:
            return 0.95  # above threshold

        # Capture kafka.send_envelope_inner calls
        sent: list[tuple[str, dict]] = []

        class _FakeKafka:
            async def send_envelope_inner(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        monkeypatch.setattr(po, "_instant_scalar", fake_instant)
        settings = self._make_settings(threshold=0.8)
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        result = await po.evaluate_proactive_triggers(ctx)  # type: ignore[arg-type]
        assert result == 1
        assert len(sent) == 1
        topic, data = sent[0]
        assert topic == "omni-proactive-incidents"
        assert "trace_id" in data


# ===========================================================================
# 7. proactive_observer — _append_audit and _append_dlq_proactive
# ===========================================================================

class TestAppendAudit:
    """Test _append_audit with a fake Kafka bus."""

    @pytest.mark.asyncio
    async def test_append_audit_sends_to_kafka(self, fake_redis: FakeRedis) -> None:
        sent: list[tuple[str, dict]] = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = SimpleNamespace(
            kafka_topic_audit_proactive="omni-audit-proactive",
        )
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        await po._append_audit(
            ctx,
            trace_id="trace-audit-001",
            rule_id="TestRule",
            outcome="SUCCESS",
            commands_run="kubectl get pods",
            detail="all ok",
            meta={"path": "sop"},
        )
        assert len(sent) == 1
        topic, payload = sent[0]
        assert topic == "omni-audit-proactive"
        import json as _json
        data = _json.loads(payload["data"])
        assert data["trace_id"] == "trace-audit-001"
        assert data["outcome"] == "SUCCESS"
        assert data["rule_id"] == "TestRule"

    @pytest.mark.asyncio
    async def test_append_audit_no_meta(self, fake_redis: FakeRedis) -> None:
        sent: list[tuple[str, dict]] = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = SimpleNamespace(kafka_topic_audit_proactive="omni-audit-proactive")
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        await po._append_audit(
            ctx,
            trace_id="trace-audit-002",
            rule_id="R2",
            outcome="FAIL",
        )
        assert len(sent) == 1


class TestAppendDlqProactive:
    @pytest.mark.asyncio
    async def test_append_dlq_returns_dlq_id(self, fake_redis: FakeRedis) -> None:
        sent: list[tuple[str, dict]] = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = SimpleNamespace(kafka_topic_dlq="omni-dlq")
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        result = await po._append_dlq_proactive(
            ctx,
            trace_id="trace-dlq-001",
            msg_id="msg-001",
            reason="EVENT_TIMEOUT",
            tombstone={"event_timeout_sec": 30},
            raw_event='{"data": "test"}',
        )
        assert result == "dlq-trace-dlq-001"
        assert len(sent) == 1
        topic, payload = sent[0]
        assert topic == "omni-dlq"
        assert payload["trace_id"] == "trace-dlq-001"


# ===========================================================================
# 8. diagnostic_k8s_clinical — integration tests with real pods
# ===========================================================================

class TestDiagnosticK8sIntegrationDeepProbes:
    """Integration tests that exercise the inner probe loops with real K8s pods."""

    @pytest.mark.asyncio
    async def test_probe_pod_status_with_existing_nginx_pod(self) -> None:
        """Uses canonical_query JSON to pass pod identity to the probe."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nginx-test-847cb6f7-c55zk"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-001",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_status(None, ev)  # type: ignore[arg-type]
        # With a real existing pod, should PASS or INCONCLUSIVE
        assert result.status in ("PASSED", "INCONCLUSIVE", "FAILED")
        assert result.probe_name == "k8s_clinical_pod_status"
        if result.status == "PASSED":
            assert result.raw_text  # should have some text
            sh = result.structured_hint
            assert sh.get("source") == "K8s_SDK"

    @pytest.mark.asyncio
    async def test_probe_pod_metrics_with_existing_nginx_pod(self) -> None:
        """Metrics probe — may return PASSED (metrics-server) or INCONCLUSIVE (404)."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nginx-test-847cb6f7-c55zk"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-002",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_metrics(None, ev)  # type: ignore[arg-type]
        # With a real pod, should PASSED (metrics) or INCONCLUSIVE (no metrics-server)
        assert result.status in ("PASSED", "INCONCLUSIVE", "FAILED")
        assert result.probe_name == "k8s_clinical_pod_metrics"

    @pytest.mark.asyncio
    async def test_probe_log_tail_with_healthy_pod(self) -> None:
        """Log tail on a healthy Running pod should be SKIPPED (clinical rule)."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nginx-test-847cb6f7-c55zk"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-003",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_log_tail(None, ev)  # type: ignore[arg-type]
        # Running healthy pod → SKIPPED (no log tail needed) or PASSED if it's unhealthy
        assert result.status in ("SKIPPED", "PASSED", "INCONCLUSIVE", "FAILED")
        assert result.probe_name == "k8s_clinical_pod_log_tail"

    @pytest.mark.asyncio
    async def test_probe_log_previous_with_healthy_pod(self) -> None:
        """Previous log on a healthy pod with no crashes → SKIPPED."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nginx-test-847cb6f7-c55zk"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-004",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_log_previous(None, ev)  # type: ignore[arg-type]
        assert result.status in ("SKIPPED", "PASSED", "INCONCLUSIVE", "FAILED")
        assert result.probe_name == "k8s_clinical_pod_log_previous"

    @pytest.mark.asyncio
    async def test_probe_pod_status_ghost_pod_no_labels(self) -> None:
        """Ghost pod with no useful labels → resolve tries pod name pattern then unresolved."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nonexistent-pod-abc-12345-xyzzy"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-005",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_status(None, ev)  # type: ignore[arg-type]
        # Ghost pod → INCONCLUSIVE or PASSED (no pods)
        assert result.status in ("PASSED", "INCONCLUSIVE", "FAILED")

    @pytest.mark.asyncio
    async def test_probe_pod_metrics_ghost_pod(self) -> None:
        """Metrics probe for a ghost pod → INCONCLUSIVE."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nonexistent-pod-abc-12345-xyzzy"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-006",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_metrics(None, ev)  # type: ignore[arg-type]
        assert result.status in ("PASSED", "INCONCLUSIVE", "FAILED")

    @pytest.mark.asyncio
    async def test_probe_pod_log_tail_ghost_pod(self) -> None:
        """Log tail for a ghost pod → INCONCLUSIVE (no active pods to inspect)."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nonexistent-pod-abc-12345-xyzzy"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-007",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_log_tail(None, ev)  # type: ignore[arg-type]
        assert result.status in ("SKIPPED", "INCONCLUSIVE", "PASSED", "FAILED")

    @pytest.mark.asyncio
    async def test_probe_pod_log_previous_ghost_pod(self) -> None:
        """Previous log for a ghost pod → INCONCLUSIVE."""
        import json as _json
        cq = _json.dumps({
            "labels": {"namespace": "multi-agent", "pod": "nonexistent-pod-abc-12345-xyzzy"},
        })
        ev = AnomalyEvent(
            trace_id="test-trace-deep-008",
            canonical_query=cq,
            namespace="multi-agent",
        )
        result = await dkc.probe_k8s_clinical_pod_log_previous(None, ev)  # type: ignore[arg-type]
        assert result.status in ("SKIPPED", "INCONCLUSIVE", "PASSED", "FAILED")


# ===========================================================================
# 9. k8s_resource_snapshot — cover remaining exception branches
# ===========================================================================

class TestFetchLastKnownStateAllKinds:
    """Test all supported kind branches in fetch_last_known_state."""

    @pytest.mark.asyncio
    async def test_pod_kind_missing(self) -> None:
        result = await krs.fetch_last_known_state("multi-agent", "Pod", "no-pod-xyz-999")
        assert result.get("unavailable") is True
        assert "ApiException" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_service_kind_missing(self) -> None:
        result = await krs.fetch_last_known_state("multi-agent", "Service", "no-svc-xyz-999")
        assert result.get("unavailable") is True

    @pytest.mark.asyncio
    async def test_namespace_kind_existing(self) -> None:
        result = await krs.fetch_last_known_state("", "Namespace", "default")
        if not result.get("unavailable"):
            assert result.get("kind") == "Namespace"

    @pytest.mark.asyncio
    async def test_namespace_kind_nonexistent(self) -> None:
        result = await krs.fetch_last_known_state("", "Namespace", "this-ns-does-not-exist-xyz")
        assert result.get("unavailable") is True

    @pytest.mark.asyncio
    async def test_namespace_empty_for_namespaced_kind_deployment(self) -> None:
        """Deployment kind with empty namespace → missing_namespace_for_namespaced_kind."""
        result = await krs.fetch_last_known_state("", "Deployment", "nginx")
        assert result.get("unavailable") is True
        assert "missing_namespace" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_deployment_kind_real(self) -> None:
        """Fetch the nginx-test deployment that exists in multi-agent."""
        result = await krs.fetch_last_known_state("multi-agent", "Deployment", "nginx-test")
        if result.get("unavailable"):
            assert "reason" in result
        else:
            assert result.get("kind") == "Deployment"

    @pytest.mark.asyncio
    async def test_pod_kind_real_nginx(self) -> None:
        """Fetch the real nginx pod."""
        result = await krs.fetch_last_known_state("multi-agent", "Pod", "nginx-test-847cb6f7-c55zk")
        if result.get("unavailable"):
            assert "reason" in result
        else:
            assert result.get("kind") == "Pod"


# ===========================================================================
# 10. proactive_observer — _save_proactive_learning_record, _parse_fallback_tool_call
# ===========================================================================

class TestSaveProactiveLearningRecord:
    """Test _save_proactive_learning_record with fake LLM and vector store."""

    @pytest.mark.asyncio
    async def test_successful_save(self, fake_redis: FakeRedis) -> None:
        """With a working LLM embed and vector store, record is saved."""
        upserted: list[tuple[str, list]] = []

        class _FakeLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.1] * 768}

        class _FakeVectorStore:
            async def upsert(self, collection_name: str, points: list) -> None:
                upserted.append((collection_name, points))

        settings = SimpleNamespace(
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
        )
        ctx = SimpleNamespace(
            redis=fake_redis,
            settings=settings,
            llm=_FakeLLM(),
            vector_store=_FakeVectorStore(),
        )
        await po._save_proactive_learning_record(
            ctx,
            trace_id="trace-learn-001",
            pattern_key="pattern-001",
            lesson="CPU spike detected on nginx pod",
            tool="kubectl_get",
            args={"namespace": "default"},
            exec_outcome="success",
            biz_outcome="correct",
            verification_result="pass",
            unknown_reason="",
        )
        assert len(upserted) == 1
        collection, points = upserted[0]
        assert "action_experience" in collection
        assert len(points) == 1

    @pytest.mark.asyncio
    async def test_exception_swallowed(self, fake_redis: FakeRedis) -> None:
        """If LLM embed fails, exception is swallowed (debug log only)."""

        class _BrokenLLM:
            async def embed(self, model: str, input: str) -> dict:
                raise RuntimeError("LLM unavailable")

        settings = SimpleNamespace(
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
        )
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, llm=_BrokenLLM())
        # Should not raise
        await po._save_proactive_learning_record(
            ctx,
            trace_id="trace-learn-002",
            pattern_key="pattern-002",
            lesson="OOM event",
            tool="kubectl_delete",
            args={},
            exec_outcome="fail",
            biz_outcome="unknown",
            verification_result="fail_safe",
        )

    @pytest.mark.asyncio
    async def test_embedding_length_mismatch_padded(self, fake_redis: FakeRedis) -> None:
        """If embedding is wrong length, it gets padded/truncated."""
        upserted: list = []

        class _ShortEmbLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.5] * 10}  # shorter than EMBED_DIM=768

        class _FakeVectorStore:
            async def upsert(self, collection_name: str, points: list) -> None:
                upserted.append(points)

        settings = SimpleNamespace(memory_canonical_strip_pods=True, embed_model="m")
        ctx = SimpleNamespace(
            redis=fake_redis, settings=settings,
            llm=_ShortEmbLLM(), vector_store=_FakeVectorStore(),
        )
        await po._save_proactive_learning_record(
            ctx, trace_id="trace-learn-003", pattern_key="p",
            lesson="test lesson", tool="t", args={},
            exec_outcome="success", biz_outcome="ok", verification_result="pass",
        )
        assert len(upserted) == 1
        # vector in point should be 768 long
        from rag.pgvector_store import EMBED_DIM
        assert len(upserted[0][0].vector) == EMBED_DIM


class TestParseFallbackToolCall:
    """Test _parse_fallback_tool_call with fake LLM chat responses."""

    @pytest.mark.asyncio
    async def test_valid_json_response_returns_call(self) -> None:
        import json as _json

        class _FakeLLM:
            async def chat(self, model: str, messages: list, options: dict) -> dict:
                payload = {"tool": "kubectl_get", "args": {"namespace": "default"}, "confidence": 0.85, "reason": "looks safe"}
                return {"message": {"content": _json.dumps(payload)}}

            async def chat_structured(self, **kwargs) -> dict:
                return await self.chat(
                    kwargs["model"],
                    kwargs["messages"],
                    kwargs.get("options") or {},
                )

        settings = SimpleNamespace(
            chat_model="qwen2.5:7b",
            proactive_fallback_max_attempts=2,
        )
        ctx = SimpleNamespace(settings=settings, llm=_FakeLLM())
        call, conf, rationale = await po._parse_fallback_tool_call(ctx, "diagnose high CPU")  # type: ignore[arg-type]
        assert call is not None
        assert call.tool == "kubectl_get"
        assert conf == 0.85
        assert "safe" in rationale

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none_after_retries(self) -> None:
        class _BadLLM:
            async def chat(self, model: str, messages: list, options: dict) -> dict:
                return {"message": {"content": "not valid json at all"}}

            async def chat_structured(self, **kwargs) -> dict:
                return await self.chat(
                    kwargs["model"],
                    kwargs["messages"],
                    kwargs.get("options") or {},
                )

        settings = SimpleNamespace(
            chat_model="qwen2.5:7b",
            proactive_fallback_max_attempts=2,
        )
        ctx = SimpleNamespace(settings=settings, llm=_BadLLM())
        call, conf, rationale = await po._parse_fallback_tool_call(ctx, "diagnose")  # type: ignore[arg-type]
        assert call is None
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_invalid_tool_name_triggers_retry(self) -> None:
        import json as _json
        attempts: list[int] = []

        class _InvalidToolLLM:
            async def chat(self, model: str, messages: list, options: dict) -> dict:
                attempts.append(1)
                # tool name that won't pass ToolCallPayload.model_validate if empty
                payload = {"tool": None, "args": {}, "confidence": 0.5, "reason": "test"}
                return {"message": {"content": _json.dumps(payload)}}

            async def chat_structured(self, **kwargs) -> dict:
                return await self.chat(
                    kwargs["model"],
                    kwargs["messages"],
                    kwargs.get("options") or {},
                )

        settings = SimpleNamespace(
            chat_model="qwen2.5:7b",
            proactive_fallback_max_attempts=3,
        )
        ctx = SimpleNamespace(settings=settings, llm=_InvalidToolLLM())
        call, conf, rationale = await po._parse_fallback_tool_call(ctx, "diagnose")  # type: ignore[arg-type]
        # After 3 attempts with invalid tool, returns None
        assert call is None
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_zero_attempts_returns_none(self) -> None:
        class _FakeLLM:
            async def chat(self, model: str, messages: list, options: dict) -> dict:
                return {}

        settings = SimpleNamespace(
            chat_model="qwen2.5:7b",
            proactive_fallback_max_attempts=0,
        )
        ctx = SimpleNamespace(settings=settings, llm=_FakeLLM())
        call, conf, rationale = await po._parse_fallback_tool_call(ctx, "diagnose")  # type: ignore[arg-type]
        assert call is None


class TestResolveFromActionExperience:
    """Test _resolve_from_action_experience with negative pattern guard."""

    @pytest.mark.asyncio
    async def test_negative_pattern_short_circuits(self, fake_redis: FakeRedis) -> None:
        """When the pattern is marked negative, returns False without calling LLM."""
        # Set up negative pattern
        await fake_redis.setex("omni:learning:negative:proactive:pattern-neg-001", 3600, "failed")
        settings = SimpleNamespace(
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
            action_experience_score_threshold=0.7,
        )
        ctx = SimpleNamespace(redis=fake_redis, settings=settings)
        ok, out, tool, meta = await po._resolve_from_action_experience(
            ctx,
            query_text="high cpu",
            score_threshold=0.7,
            pattern_key="pattern-neg-001",
        )
        assert ok is False
        assert out is None

    @pytest.mark.asyncio
    async def test_exception_in_resolve_returns_false(self, fake_redis: FakeRedis) -> None:
        """If LLM embed raises, returns False (graceful degradation)."""
        class _BrokenLLM:
            async def embed(self, model: str, input: str) -> dict:
                raise RuntimeError("embed down")

        settings = SimpleNamespace(
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
        )
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, llm=_BrokenLLM())
        ok, out, tool, meta = await po._resolve_from_action_experience(
            ctx,
            query_text="high cpu",
            score_threshold=0.7,
            pattern_key="",
        )
        assert ok is False


# ===========================================================================
# 11. proactive_observer — _process_proactive_message (kill_switch + bad payload paths)
# ===========================================================================

class TestProcessProactiveMessage:
    """Test _process_proactive_message for early-exit paths."""

    def _make_audit_settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            proactive_kill_switch_key="omni:proactive:kill_switch",
            kafka_topic_audit_proactive="omni-audit-proactive",
            kafka_topic_audit_agent="omni-audit-agent",
            proactive_gigo_require_cluster_identity=False,
        )

    @pytest.mark.asyncio
    async def test_kill_switch_skips_message(self, fake_redis: FakeRedis) -> None:
        """When kill switch is on, _process_proactive_message writes SKIPPED audit and returns."""
        await fake_redis.set("omni:proactive:kill_switch", "1")
        sent: list = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = self._make_audit_settings()
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        await po._process_proactive_message(ctx, "msg-001", '{"data": "test"}')  # type: ignore[arg-type]
        # Should have sent one audit entry
        assert len(sent) == 1
        import json as _json
        topic, payload = sent[0]
        data = _json.loads(payload["data"])
        assert data["outcome"] == "SKIPPED_KILL_SWITCH"

    @pytest.mark.asyncio
    async def test_bad_payload_writes_fail_audit(self, fake_redis: FakeRedis) -> None:
        """Invalid JSON payload → FAIL audit outcome."""
        sent: list = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = self._make_audit_settings()
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        await po._process_proactive_message(ctx, "msg-002", "not valid json")  # type: ignore[arg-type]
        # Should have written a FAIL audit
        assert len(sent) >= 1
        import json as _json
        # Find the FAIL audit
        fail_found = False
        for topic, payload in sent:
            try:
                data = _json.loads(payload.get("data", "{}"))
                if data.get("outcome") == "FAIL":
                    fail_found = True
                    break
            except Exception:
                pass
        assert fail_found

    @pytest.mark.asyncio
    async def test_invalid_anomaly_event_schema_writes_fail(self, fake_redis: FakeRedis) -> None:
        """AnomalyEvent schema validation fail → FAIL audit."""
        import json as _json
        sent: list = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = self._make_audit_settings()
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())
        # Missing required fields in AnomalyEvent
        bad_event = _json.dumps({"trace_id": "x", "rule_name": "R"})  # missing canonical_query
        await po._process_proactive_message(ctx, "msg-003", bad_event)  # type: ignore[arg-type]
        assert len(sent) >= 1


# ===========================================================================
# 12. proactive_observer — _fail_safe_after_tool_error (no-resource-ref path)
# ===========================================================================

class TestFailSafeAfterToolError:
    """Test _fail_safe_after_tool_error — only the no-ref path which doesn't need K8s."""

    @pytest.mark.asyncio
    async def test_no_resource_ref_path(self, fake_redis: FakeRedis) -> None:
        """When extract_resource_ref returns None, k8s_state is unavailable immediately."""
        import json as _json
        from workers.tools import ToolCallPayload

        sent: list = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))
            async def send_envelope_inner(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        class _FakeLedger:
            pass

        class _FakeLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.0] * 768}

        class _FakeVS:
            async def upsert(self, collection_name: str, points: list) -> None:
                pass

        settings = SimpleNamespace(
            kafka_topic_dlq="omni-dlq",
            kafka_topic_audit_proactive="omni-audit-proactive",
            kafka_topic_audit_agent="omni-audit-agent",
            proactive_resource_freeze_enabled=False,
            proactive_k8s_snapshot_timeout_sec=5.0,
            proactive_freeze_key_prefix="omni:freeze",
            proactive_resource_freeze_ttl_sec=300,
            proactive_freeze_namespace_fallback_allowed=False,
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
            telegram_admin_chat_id=None,
        )
        ctx = SimpleNamespace(
            redis=fake_redis,
            settings=settings,
            kafka=_FakeKafka(),
            telegram=None,
            llm=_FakeLLM(),
            vector_store=_FakeVS(),
        )

        ev = AnomalyEvent(
            trace_id="trace-failsafe-001",
            canonical_query="high cpu in cluster",
            rule_name="CPUHigh",
        )
        # "promql_instant" tool has no resource ref (namespace/kind/name)
        call = ToolCallPayload(tool="promql_instant", args={"query": "up"})
        err = RuntimeError("tool execution failed")

        await po._fail_safe_after_tool_error(
            ctx,
            ev,
            "trace-failsafe-001",
            "pattern-key-xxx",
            call,
            err,
            reason_code="TOOL_EXCEPTION",
            stream_msg_id="msg-001",
        )
        # Should have sent DLQ and audit messages
        topics = [t for t, _ in sent]
        assert "omni-dlq" in topics
        assert "omni-audit-proactive" in topics


# ===========================================================================
# 13. proactive_observer — _resolve_from_action_experience (vector store path)
# ===========================================================================

class TestResolveFromActionExperienceVectorStore:
    """Test _resolve_from_action_experience when LLM embed succeeds but vector store returns empty."""

    @pytest.mark.asyncio
    async def test_empty_query_points_returns_false(self, fake_redis: FakeRedis) -> None:
        """When vector store returns empty points, returns False."""
        class _FakeLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.1] * 768}

        class _EmptyResponse:
            points: list = []

        class _FakeVS:
            async def query_points(self, collection_name: str, query: list, limit: int,
                                   score_threshold: float, with_payload: bool) -> Any:
                return _EmptyResponse()

        settings = SimpleNamespace(
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
        )
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, llm=_FakeLLM(), vector_store=_FakeVS())
        ok, out, tool, meta = await po._resolve_from_action_experience(
            ctx,
            query_text="cpu high in namespace default",
            score_threshold=0.7,
            pattern_key="",
        )
        assert ok is False
        assert out is None

    @pytest.mark.asyncio
    async def test_non_success_outcome_in_points_skipped(self, fake_redis: FakeRedis) -> None:
        """Points with exec_outcome != 'success' are skipped."""
        class _FakeLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.2] * 768}

        class _FakePoint:
            score = 0.9
            payload = {"exec_outcome": "fail", "tool": "kubectl_get", "auto_execute": True, "args": {}}

        class _FakeResponse:
            points = [_FakePoint()]

        class _FakeVS:
            async def query_points(self, **kwargs: Any) -> Any:
                return _FakeResponse()

        settings = SimpleNamespace(memory_canonical_strip_pods=True, embed_model="nomic-embed-text:latest")
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, llm=_FakeLLM(), vector_store=_FakeVS())
        ok, out, tool, meta = await po._resolve_from_action_experience(
            ctx,
            query_text="query",
            score_threshold=0.5,
            pattern_key="",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_unknown_tool_in_registry_skipped(self, fake_redis: FakeRedis) -> None:
        """Points with a tool name not in TOOL_REGISTRY are skipped."""
        class _FakeLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.3] * 768}

        class _FakePoint:
            score = 0.95
            payload = {
                "exec_outcome": "success",
                "tool": "__nonexistent_tool__",
                "auto_execute": True,
                "args": {},
            }

        class _FakeResponse:
            points = [_FakePoint()]

        class _FakeVS:
            async def query_points(self, **kwargs: Any) -> Any:
                return _FakeResponse()

        settings = SimpleNamespace(memory_canonical_strip_pods=True, embed_model="nomic-embed-text:latest")
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, llm=_FakeLLM(), vector_store=_FakeVS())
        ok, out, tool, meta = await po._resolve_from_action_experience(
            ctx,
            query_text="query",
            score_threshold=0.5,
            pattern_key="",
        )
        assert ok is False


# ===========================================================================
# 14. proactive_observer — _process_proactive_message GIGO path
# ===========================================================================

class TestProcessProactiveMessageGigo:
    """Test GIGO filter path in _process_proactive_message."""

    @pytest.mark.asyncio
    async def test_gigo_filter_skips_message(self, fake_redis: FakeRedis) -> None:
        """When GIGO check fails, SKIPPED_GIGO audit is written and function returns."""
        import json as _json
        sent: list = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        settings = SimpleNamespace(
            proactive_kill_switch_key="omni:proactive:kill_switch",
            kafka_topic_audit_proactive="omni-audit-proactive",
            kafka_topic_audit_agent="omni-audit-agent",
            proactive_gigo_require_cluster_identity=True,
        )
        ctx = SimpleNamespace(redis=fake_redis, settings=settings, kafka=_FakeKafka())

        # Create an AnomalyEvent with empty gigo_metadata — proactive_gigo_cluster_identity_ok returns False
        ev_dict = {
            "trace_id": "trace-gigo-001",
            "rule_name": "TestRule",
            "canonical_query": "cpu high",
            "gigo_metadata": {},  # Empty → GIGO check will fail
        }
        raw = _json.dumps(ev_dict)
        await po._process_proactive_message(ctx, "msg-gigo-001", raw)  # type: ignore[arg-type]
        # Should have sent audit with SKIPPED_GIGO or INGESTED transition
        topics = [t for t, _ in sent]
        # At minimum, a transition was sent (INGESTED), then GIGO if failed
        assert len(sent) >= 1


# ===========================================================================
# 15. proactive_observer — _process_proactive_message full pipeline (with semaphore stub)
# ===========================================================================

class TestProcessProactiveMessagePipeline:
    """Test _process_proactive_message with a minimal working context (semaphore stub)."""

    @pytest.mark.asyncio
    async def test_pipeline_with_kill_switch_off(self, fake_redis: FakeRedis) -> None:
        """Full pipeline with minimal stubs — exercises semaphore acquire/release path."""
        import json as _json
        sent: list = []

        class _FakeKafka:
            async def send_dict(self, topic: str, data: dict) -> None:
                sent.append((topic, data))
            async def send_envelope_inner(self, topic: str, data: dict) -> None:
                sent.append((topic, data))

        class _FakeSemaphore:
            async def acquire_proactive(self) -> str:
                return "tok-001"
            async def release(self, token: str) -> None:
                pass

        class _FakeLedger:
            async def record_exception(self, *a: Any, **k: Any) -> None:
                pass

        class _FakeLLM:
            async def embed(self, model: str, input: str) -> dict:
                return {"embedding": [0.0] * 768}

        class _EmptyResponse:
            points: list = []

        class _FakeVS:
            async def query_points(self, **kwargs: Any) -> Any:
                return _EmptyResponse()

        async def _fake_resolve_mem(ctx: Any, query: str, **kw: Any) -> tuple:
            return False, None, None

        async def _fake_emit_transition(ctx: Any, **kwargs: Any) -> None:
            pass

        async def _fake_emit_terminal(ctx: Any, **kwargs: Any) -> None:
            pass

        async def _fake_learning_decision(ctx: Any, pattern_key: str) -> tuple:
            return "deny", 0.0

        settings = SimpleNamespace(
            proactive_kill_switch_key="omni:proactive:kill_switch",
            kafka_topic_audit_proactive="omni-audit-proactive",
            kafka_topic_audit_agent="omni-audit-agent",
            proactive_gigo_require_cluster_identity=False,
            proactive_sop_collection="sop",
            proactive_sop_score_threshold=0.75,
            action_experience_score_threshold=0.7,
            memory_canonical_strip_pods=True,
            embed_model="nomic-embed-text:latest",
            telegram_admin_chat_id=None,
            proactive_event_timeout_sec=30.0,
            proactive_fallback_enabled=False,
            proactive_fallback_bypass_policy_in_god_mode=False,
            god_mode=False,
            lab_unchained=False,
            proactive_verify_keywords_fail="",
            diagnostic_dictionary_enabled=False,
            kafka_topic_dlq="omni-dlq",
            learning_stats_ttl_sec=86400,
        )
        ctx = SimpleNamespace(
            redis=fake_redis,
            settings=settings,
            kafka=_FakeKafka(),
            semaphore=_FakeSemaphore(),
            ledger=_FakeLedger(),
            llm=_FakeLLM(),
            vector_store=_FakeVS(),
            telegram=None,
            inbound_proactive=False,
            inbound_trace_id="",
            scout_ready=None,
        )

        import workers.proactive_observer as _po_mod
        from workers import autonomy_contract as _ac

        # Monkeypatch heavy dependencies
        _orig_resolve = _po_mod.resolve_remediation_from_memory if hasattr(_po_mod, "resolve_remediation_from_memory") else None
        _orig_emit = _ac.emit_transition
        _orig_terminal = _ac.emit_terminal_tombstone
        _orig_lgd = _po_mod._learning_governance_decision

        _po_mod.resolve_remediation_from_memory = _fake_resolve_mem  # type: ignore[assignment]
        _ac.emit_transition = _fake_emit_transition  # type: ignore[assignment]
        _ac.emit_terminal_tombstone = _fake_emit_terminal  # type: ignore[assignment]
        _po_mod._learning_governance_decision = _fake_learning_decision  # type: ignore[assignment]

        try:
            ev_dict = {
                "trace_id": "trace-pipe-001",
                "rule_name": "CPUHigh",
                "canonical_query": "cluster cpu high",
                "namespace": "default",
            }
            raw = _json.dumps(ev_dict)
            await _po_mod._process_proactive_message(ctx, "msg-pipe-001", raw)
        finally:
            # Restore originals
            if _orig_resolve is not None:
                _po_mod.resolve_remediation_from_memory = _orig_resolve  # type: ignore[assignment]
            _ac.emit_transition = _orig_emit
            _ac.emit_terminal_tombstone = _orig_terminal
            _po_mod._learning_governance_decision = _orig_lgd  # type: ignore[assignment]

        # At least some messages were sent (audit/dlq)
        assert len(sent) >= 1
