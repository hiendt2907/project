"""Unit tests for blast-radius diff-scoring + impact-tree cascading deletes (plan step 3)."""

from __future__ import annotations

import asyncio

import pytest

from pkg.executor.blast_radius import assess_blast_radius


class FakeReader:
    def __init__(self, *, pods_by_selector=None, ns_pods=None, replicas=None, pvc=None, selectors=None):
        self._by_sel = pods_by_selector or {}
        self._ns_pods = ns_pods or []
        self._replicas = replicas or {}
        self._pvc = pvc or {}
        self._selectors = selectors or {}

    async def list_pod_names(self, namespace, label_selector=None):
        if label_selector is None:
            return list(self._ns_pods)
        return list(self._by_sel.get(label_selector, []))

    async def workload_selector(self, namespace, kind, name):
        return self._selectors.get((kind, name))

    async def workload_replicas(self, namespace, kind, name):
        return self._replicas.get((kind, name))

    async def workload_has_pvc(self, namespace, kind, name):
        return self._pvc.get((kind, name), False)


class TestScaleDown:
    async def test_scale_to_zero_hard_blocks(self):
        reader = FakeReader(replicas={("Deployment", "payment"): 4})
        v = await assess_blast_radius(
            reader, tool="k8s_scale_deployment",
            args={"namespace": "multi-agent", "name": "payment", "replicas": 0},
        )
        assert v.hard_block
        assert v.affected_pods == 4
        assert v.capacity_drop_pct == 100.0

    async def test_small_scale_down_allowed(self):
        reader = FakeReader(replicas={("Deployment", "web"): 10})
        v = await assess_blast_radius(
            reader, tool="k8s_scale_deployment",
            args={"namespace": "multi-agent", "name": "web", "replicas": 9},
            max_pods=10, capacity_drop_pct=20.0,
        )
        assert v.allow
        assert v.affected_pods == 1

    async def test_statefulset_storage_blocks(self):
        reader = FakeReader(replicas={("StatefulSet", "pg"): 3}, pvc={("StatefulSet", "pg"): True})
        v = await assess_blast_radius(
            reader, tool="k8s_scale_deployment",
            args={"namespace": "multi-agent", "kind": "StatefulSet", "name": "pg", "replicas": 2},
        )
        assert v.hard_block
        assert v.touches_storage


class TestCascadingDelete:
    async def test_delete_namespace_cascades_all_pods(self):
        reader = FakeReader(ns_pods=[f"p{i}" for i in range(30)])
        v = await assess_blast_radius(
            reader, tool="kubectl_cluster",
            args={"verb": "delete", "kind": "Namespace", "name": "multi-agent"},
        )
        assert v.hard_block
        assert v.namespace_wide
        assert v.affected_pods == 30
        assert any("namespace" in r.lower() for r in v.reasons)

    async def test_delete_deployment_cascades_child_pods(self):
        reader = FakeReader(
            selectors={("Deployment", "api"): "app=api"},
            pods_by_selector={"app=api": ["api-1", "api-2", "api-3", "api-4", "api-5",
                                          "api-6", "api-7", "api-8", "api-9", "api-10", "api-11"]},
        )
        v = await assess_blast_radius(
            reader, tool="kubectl_cluster",
            args={"namespace": "multi-agent", "verb": "delete", "kind": "Deployment", "name": "api"},
        )
        assert v.hard_block  # 11 > default max 10
        assert v.affected_pods == 11
        assert any("cascade" in r.lower() for r in v.reasons)

    async def test_delete_single_pod_low_impact(self):
        reader = FakeReader()
        v = await assess_blast_radius(
            reader, tool="kubectl_cluster",
            args={"namespace": "multi-agent", "verb": "delete", "kind": "Pod", "name": "nginx-x"},
        )
        assert v.allow
        assert v.affected_pods == 1


class TestConfigChange:
    async def test_configmap_patch_counts_rolling_restart(self):
        reader = FakeReader(
            selectors={("Deployment", "web"): "app=web"},
            pods_by_selector={"app=web": [f"web-{i}" for i in range(15)]},
        )
        v = await assess_blast_radius(
            reader, tool="k8s_patch_configmap",
            args={"namespace": "multi-agent", "name": "web"},
        )
        assert v.hard_block
        assert v.affected_pods == 15


class TestFailClosed:
    async def test_no_reader_blocks_destructive(self):
        v = await assess_blast_radius(None, tool="k8s_scale_deployment", args={"replicas": 0})
        assert v.hard_block

    async def test_no_reader_allows_rollout_restart(self):
        v = await assess_blast_radius(None, tool="k8s_rollout_restart", args={"name": "web"})
        assert v.allow


# ── K8sBlastReader — timeout + circuit breaker (task #21) ────────────────────
# A fresh K8sBlastReader is constructed per mutate attempt (autonomous_execute.py),
# so the breaker lives at module scope (`blast_radius._circuit`) shared across
# instances within the process — reset it before/after each test here.
class TestK8sApiCircuitBreaker:
    @pytest.fixture(autouse=True)
    def _reset_circuit(self):
        from pkg.executor import blast_radius as br

        br._circuit = br._K8sApiCircuitBreaker()
        yield
        br._circuit = br._K8sApiCircuitBreaker()

    def test_allows_calls_when_closed(self):
        from pkg.executor.blast_radius import _K8sApiCircuitBreaker

        cb = _K8sApiCircuitBreaker()
        assert cb.allow() is True

    def test_opens_after_threshold_consecutive_failures(self):
        from pkg.executor.blast_radius import _CIRCUIT_FAILURE_THRESHOLD, _K8sApiCircuitBreaker

        cb = _K8sApiCircuitBreaker()
        for _ in range(_CIRCUIT_FAILURE_THRESHOLD - 1):
            cb.record_failure()
            assert cb.allow() is True  # not yet at threshold
        cb.record_failure()
        assert cb.allow() is False

    def test_success_resets_failure_count(self):
        from pkg.executor.blast_radius import _CIRCUIT_FAILURE_THRESHOLD, _K8sApiCircuitBreaker

        cb = _K8sApiCircuitBreaker()
        for _ in range(_CIRCUIT_FAILURE_THRESHOLD - 1):
            cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.allow() is True  # counter reset by the success, back to 1/threshold

    def test_half_open_after_cooldown(self, monkeypatch):
        from pkg.executor.blast_radius import _CIRCUIT_FAILURE_THRESHOLD, _K8sApiCircuitBreaker

        cb = _K8sApiCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("pkg.executor.blast_radius.time.monotonic", lambda: t[0])
        for _ in range(_CIRCUIT_FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.allow() is False
        t[0] += 60.0  # past _CIRCUIT_COOLDOWN_SEC
        assert cb.allow() is True

    def test_reader_init_raises_when_circuit_open(self):
        from pkg.executor import blast_radius as br

        for _ in range(br._CIRCUIT_FAILURE_THRESHOLD):
            br._circuit.record_failure()
        with pytest.raises(RuntimeError, match="circuit breaker open"):
            br.K8sBlastReader()

    async def test_call_with_timeout_records_failure_and_reraises(self, monkeypatch):
        from pkg.executor import blast_radius as br

        monkeypatch.setattr(br, "K8S_API_TIMEOUT_SEC", 0.05)

        async def _hang() -> None:
            await asyncio.sleep(1)

        with pytest.raises(asyncio.TimeoutError):
            await br.K8sBlastReader._call_with_timeout(_hang())
        assert br._circuit._consecutive_failures == 1

    async def test_call_with_timeout_records_success(self):
        from pkg.executor.blast_radius import K8sBlastReader, _circuit

        async def _ok() -> str:
            return "value"

        _circuit._consecutive_failures = 2
        result = await K8sBlastReader._call_with_timeout(_ok())
        assert result == "value"
        assert _circuit._consecutive_failures == 0
