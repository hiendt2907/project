"""Coverage tests for workers.diagnostic_k8s_clinical — targets uncovered async paths."""
from __future__ import annotations

import os
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")

from workers import diagnostic_k8s_clinical as dkc
from workers.diagnostic_evidence import ProbeRunRaw
from workers.proactive_models import AnomalyEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns(fields: dict[str, Any]) -> SimpleNamespace:
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
    name: str = "test-pod-abc12-xyzab",
    phase: str = "Running",
    conditions: list[dict] | None = None,
    container_statuses: list[dict] | None = None,
    containers: list[dict] | None = None,
    owner_references: list[dict] | None = None,
) -> SimpleNamespace:
    cond_list = [
        _ns({"type": c.get("type", "Ready"), "status": c.get("status", "True"), "reason": c.get("reason", "")})
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
    return SimpleNamespace(
        metadata=_ns({"owner_references": owner_refs, "name": name}),
        spec=_ns({"containers": spec_containers}),
        status=_ns({
            "phase": phase,
            "conditions": cond_list,
            "container_statuses": cs_list,
        }),
    )


def _make_ev(
    namespace: str = "multi-agent",
    pod: str = "my-svc-abc12-xyzab",
    deployment: str = "my-svc",
) -> AnomalyEvent:
    return AnomalyEvent(
        trace_id="test-trace-001",
        canonical_query="{}",
        namespace=namespace,
        deployment=deployment,
        gigo_metadata={"pod": pod},
    )


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.settings = SimpleNamespace(
        k8s_default_namespace="multi-agent",
        prometheus_url="http://prometheus:9090",
        kafka_topic_alerts="omni-alerts",
        kafka_bootstrap_servers="localhost:9092",
    )
    ctx.redis = AsyncMock()
    ctx.kafka = AsyncMock()
    return ctx


def _fake_api_client() -> MagicMock:
    ac = MagicMock()
    ac.close = AsyncMock()
    return ac


def _mock_k8s_apis():
    """Return (v1, apps, batch_api) mock trio with closeable api_client."""
    v1 = AsyncMock()
    v1.api_client = _fake_api_client()
    apps = AsyncMock()
    apps.api_client = _fake_api_client()
    batch_api = AsyncMock()
    batch_api.api_client = _fake_api_client()
    return v1, apps, batch_api


# ---------------------------------------------------------------------------
# _follow_top_controller
# ---------------------------------------------------------------------------

class TestFollowTopController:
    async def test_max_depth_exceeded(self):
        apps = AsyncMock()
        result = await dkc._follow_top_controller(apps, "ns", "ReplicaSet", "rs", 6)
        assert result is None

    async def test_deployment_returns_immediately(self):
        apps = AsyncMock()
        result = await dkc._follow_top_controller(apps, "ns", "Deployment", "my-dep", 0)
        assert result == ("Deployment", "my-dep")

    async def test_statefulset_returns_immediately(self):
        apps = AsyncMock()
        result = await dkc._follow_top_controller(apps, "ns", "StatefulSet", "my-sts", 0)
        assert result == ("StatefulSet", "my-sts")

    async def test_daemonset_returns_immediately(self):
        apps = AsyncMock()
        result = await dkc._follow_top_controller(apps, "ns", "DaemonSet", "my-ds", 0)
        assert result == ("DaemonSet", "my-ds")

    async def test_replicaset_follows_owner_to_deployment(self):
        rs = _ns({
            "metadata": {
                "owner_references": [_ns({"kind": "Deployment", "name": "my-dep"})]
            }
        })
        apps = AsyncMock()
        apps.read_namespaced_replica_set = AsyncMock(return_value=rs)
        result = await dkc._follow_top_controller(apps, "ns", "ReplicaSet", "my-rs", 0)
        assert result == ("Deployment", "my-dep")

    async def test_replicaset_no_owner_returns_none(self):
        rs = _ns({"metadata": {"owner_references": []}})
        apps = AsyncMock()
        apps.read_namespaced_replica_set = AsyncMock(return_value=rs)
        result = await dkc._follow_top_controller(apps, "ns", "ReplicaSet", "orphan-rs", 0)
        assert result is None

    async def test_replicaset_api_exception_returns_none(self):
        from kubernetes_asyncio.client import ApiException
        apps = AsyncMock()
        apps.read_namespaced_replica_set = AsyncMock(side_effect=ApiException(status=500))
        result = await dkc._follow_top_controller(apps, "ns", "ReplicaSet", "rs", 0)
        assert result is None

    async def test_unknown_kind_returns_none(self):
        apps = AsyncMock()
        result = await dkc._follow_top_controller(apps, "ns", "Pod", "some-pod", 0)
        assert result is None


# ---------------------------------------------------------------------------
# _top_controller_from_pod
# ---------------------------------------------------------------------------

class TestTopControllerFromPod:
    async def test_no_owner_references(self):
        pod = _fake_pod()
        apps = AsyncMock()
        result = await dkc._top_controller_from_pod(apps, "ns", pod)
        assert result is None

    async def test_owner_reference_not_controller(self):
        pod = _fake_pod(owner_references=[{"kind": "Deployment", "name": "my-dep", "controller": False}])
        apps = AsyncMock()
        result = await dkc._top_controller_from_pod(apps, "ns", pod)
        assert result is None

    async def test_finds_deployment_via_replicaset(self):
        rs = _ns({"metadata": {"owner_references": [_ns({"kind": "Deployment", "name": "my-dep"})]}})
        apps = AsyncMock()
        apps.read_namespaced_replica_set = AsyncMock(return_value=rs)
        pod = _fake_pod(owner_references=[{"kind": "ReplicaSet", "name": "my-rs", "controller": True}])
        result = await dkc._top_controller_from_pod(apps, "ns", pod)
        assert result == ("Deployment", "my-dep")


# ---------------------------------------------------------------------------
# _list_pod_names_for_workload
# ---------------------------------------------------------------------------

class TestListPodNamesForWorkload:
    def _make_pod_list(self, names: list[str]) -> SimpleNamespace:
        items = [_ns({"metadata": {"name": n}}) for n in names]
        return SimpleNamespace(items=items)

    async def test_deployment_path(self):
        v1, apps, batch_api = _mock_k8s_apis()
        dep = _ns({
            "spec": {"selector": {"match_labels": {"app": "my-app"}}}
        })
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        v1.list_namespaced_pod = AsyncMock(return_value=self._make_pod_list(["pod-a", "pod-b"]))
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "Deployment", "my-dep")
        assert sorted(result) == ["pod-a", "pod-b"]

    async def test_statefulset_path(self):
        v1, apps, batch_api = _mock_k8s_apis()
        sts = _ns({"spec": {"selector": {"match_labels": {"app": "my-sts"}}}})
        apps.read_namespaced_stateful_set = AsyncMock(return_value=sts)
        v1.list_namespaced_pod = AsyncMock(return_value=self._make_pod_list(["sts-pod-0"]))
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "StatefulSet", "my-sts")
        assert result == ["sts-pod-0"]

    async def test_daemonset_path(self):
        v1, apps, batch_api = _mock_k8s_apis()
        ds = _ns({"spec": {"selector": {"match_labels": {"app": "my-ds"}}}})
        apps.read_namespaced_daemon_set = AsyncMock(return_value=ds)
        v1.list_namespaced_pod = AsyncMock(return_value=self._make_pod_list(["ds-pod-node1"]))
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "DaemonSet", "my-ds")
        assert result == ["ds-pod-node1"]

    async def test_job_path(self):
        v1, apps, batch_api = _mock_k8s_apis()
        job = _ns({"spec": {"selector": {"match_labels": {"job": "my-job"}}}})
        batch_api.read_namespaced_job = AsyncMock(return_value=job)
        v1.list_namespaced_pod = AsyncMock(return_value=self._make_pod_list(["job-pod-1"]))
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "Job", "my-job")
        assert result == ["job-pod-1"]

    async def test_cronjob_returns_empty(self):
        v1, apps, batch_api = _mock_k8s_apis()
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "CronJob", "my-cron")
        assert result == []

    async def test_api_exception_returns_empty(self):
        from kubernetes_asyncio.client import ApiException
        v1, apps, batch_api = _mock_k8s_apis()
        apps.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "Deployment", "missing")
        assert result == []

    async def test_no_match_labels_returns_empty(self):
        v1, apps, batch_api = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "Deployment", "dep")
        assert result == []

    async def test_list_pods_api_exception_returns_empty(self):
        from kubernetes_asyncio.client import ApiException
        v1, apps, batch_api = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {"app": "x"}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        v1.list_namespaced_pod = AsyncMock(side_effect=ApiException(status=403))
        result = await dkc._list_pod_names_for_workload(v1, apps, batch_api, "ns", "Deployment", "dep")
        assert result == []


# ---------------------------------------------------------------------------
# _try_list_by_deployment_name / _try_list_by_label_eq
# ---------------------------------------------------------------------------

class TestTryListHelpers:
    def _pod_list(self, names: list[str]) -> SimpleNamespace:
        return SimpleNamespace(items=[_ns({"metadata": {"name": n}}) for n in names])

    async def test_try_list_by_deployment_name_success(self):
        v1, apps, _ = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {"app": "svc"}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["svc-pod-1"]))
        result = await dkc._try_list_by_deployment_name(v1, apps, "ns", "svc")
        assert result == ["svc-pod-1"]

    async def test_try_list_by_deployment_name_api_exception(self):
        from kubernetes_asyncio.client import ApiException
        v1, apps, _ = _mock_k8s_apis()
        apps.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))
        result = await dkc._try_list_by_deployment_name(v1, apps, "ns", "missing")
        assert result is None

    async def test_try_list_by_deployment_name_no_selector(self):
        v1, apps, _ = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        result = await dkc._try_list_by_deployment_name(v1, apps, "ns", "dep")
        assert result is None

    async def test_try_list_by_label_eq_success(self):
        v1 = AsyncMock()
        v1.api_client = _fake_api_client()
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["p1", "p2"]))
        result = await dkc._try_list_by_label_eq(v1, "ns", "app", "my-app")
        assert sorted(result) == ["p1", "p2"]

    async def test_try_list_by_label_eq_empty_key(self):
        v1 = AsyncMock()
        result = await dkc._try_list_by_label_eq(v1, "ns", "", "value")
        assert result is None

    async def test_try_list_by_label_eq_api_exception(self):
        from kubernetes_asyncio.client import ApiException
        v1 = AsyncMock()
        v1.list_namespaced_pod = AsyncMock(side_effect=ApiException(status=403))
        result = await dkc._try_list_by_label_eq(v1, "ns", "app", "svc")
        assert result is None


# ---------------------------------------------------------------------------
# _resolve_pods_when_pod_missing
# ---------------------------------------------------------------------------

class TestResolvePodsWhenPodMissing:
    def _pod_list(self, names: list[str]) -> SimpleNamespace:
        return SimpleNamespace(items=[_ns({"metadata": {"name": n}}) for n in names])

    async def test_resolves_via_deployment_label(self):
        v1, apps, batch_api = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {"app": "svc"}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["svc-pod-1"]))
        labels = {"deployment": "svc"}
        pods, how = await dkc._resolve_pods_when_pod_missing(v1, apps, batch_api, "ns", "old-pod", labels)
        assert pods == ["svc-pod-1"]
        assert how == "label_deployment"

    async def test_resolves_via_app_label(self):
        v1, apps, batch_api = _mock_k8s_apis()
        from kubernetes_asyncio.client import ApiException
        apps.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["app-pod-1"]))
        labels = {"app": "my-app"}
        pods, how = await dkc._resolve_pods_when_pod_missing(v1, apps, batch_api, "ns", "old-pod", labels)
        assert pods == ["app-pod-1"]
        assert "label_app" in how

    async def test_resolves_via_job_name_label(self):
        v1, apps, batch_api = _mock_k8s_apis()
        job = _ns({"spec": {"selector": {"match_labels": {"job-name": "myjob"}}}})
        batch_api.read_namespaced_job = AsyncMock(return_value=job)
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["job-pod"]))
        labels = {"job-name": "myjob"}
        pods, how = await dkc._resolve_pods_when_pod_missing(v1, apps, batch_api, "ns", "old-pod", labels)
        assert pods == ["job-pod"]
        assert how == "label_job-name"

    async def test_resolves_via_pod_name_pattern(self):
        v1, apps, batch_api = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {"app": "svc"}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["svc-new-pod"]))
        pods, how = await dkc._resolve_pods_when_pod_missing(
            v1, apps, batch_api, "ns", "svc-abc1234-xyzab", {}
        )
        assert pods == ["svc-new-pod"]
        assert how == "pod_name_pattern_deployment"

    async def test_unresolved_when_all_fail(self):
        from kubernetes_asyncio.client import ApiException
        v1, apps, batch_api = _mock_k8s_apis()
        apps.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))
        apps.read_namespaced_stateful_set = AsyncMock(side_effect=ApiException(status=404))
        v1.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[]))
        pods, how = await dkc._resolve_pods_when_pod_missing(
            v1, apps, batch_api, "ns", "unknown-pod", {}
        )
        assert pods == []
        assert how == "unresolved"

    async def test_resolves_bare_workload_name_as_deployment(self):
        # F14: alert "pod" carries the bare workload name (no replica hash) and the
        # deployment/app label was stripped during canonicalization. Must still
        # resolve to live pods via the alert_pod-as-Deployment fallback, NOT collapse
        # to "unresolved" (which strands the whole state lane).
        v1, apps, batch_api = _mock_k8s_apis()
        dep = _ns({"spec": {"selector": {"match_labels": {"app": "nginx"}}}})
        apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        v1.list_namespaced_pod = AsyncMock(
            return_value=self._pod_list(["nginx-test-65b79d94c8-n4fzf"])
        )
        pods, how = await dkc._resolve_pods_when_pod_missing(
            v1, apps, batch_api, "multi-agent", "nginx-test", {}
        )
        assert pods == ["nginx-test-65b79d94c8-n4fzf"]
        assert how == "alert_pod_as_deployment"

    async def test_resolves_bare_workload_name_as_statefulset(self):
        # F14: same fallback must cover StatefulSet workloads (DB/stateful tiers).
        from kubernetes_asyncio.client import ApiException
        v1, apps, batch_api = _mock_k8s_apis()
        apps.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))
        sts = _ns({"spec": {"selector": {"match_labels": {"app": "pg"}}}})
        apps.read_namespaced_stateful_set = AsyncMock(return_value=sts)
        v1.list_namespaced_pod = AsyncMock(return_value=self._pod_list(["postgres-0"]))
        pods, how = await dkc._resolve_pods_when_pod_missing(
            v1, apps, batch_api, "multi-agent", "postgres", {}
        )
        assert pods == ["postgres-0"]
        assert how == "alert_pod_as_statefulset"


# ---------------------------------------------------------------------------
# fetch_pod_events_summary
# ---------------------------------------------------------------------------

class TestFetchPodEventsSummary:
    async def test_returns_events(self):
        ev1 = _ns({"type": "Warning", "reason": "OOMKilling", "message": "OOM killed", "last_timestamp": None, "event_time": None})
        v1 = AsyncMock()
        v1.list_namespaced_event = AsyncMock(return_value=SimpleNamespace(items=[ev1]))
        result = await dkc.fetch_pod_events_summary(v1, "ns", "pod")
        assert "Warning" in result
        assert "OOMKilling" in result

    async def test_no_events_returns_no_events_marker(self):
        v1 = AsyncMock()
        v1.list_namespaced_event = AsyncMock(return_value=SimpleNamespace(items=[]))
        result = await dkc.fetch_pod_events_summary(v1, "ns", "pod")
        assert "no events" in result

    async def test_exception_returns_error_text(self):
        v1 = AsyncMock()
        v1.list_namespaced_event = AsyncMock(side_effect=Exception("network error"))
        result = await dkc.fetch_pod_events_summary(v1, "ns", "pod")
        assert "failed" in result.lower()


# ---------------------------------------------------------------------------
# _single_pod_status_fragment
# ---------------------------------------------------------------------------

class TestSinglePodStatusFragment:
    def test_crash_loop_detected(self):
        pod = _fake_pod(
            phase="Running",
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}],
        )
        frag, structured = dkc._single_pod_status_fragment("ns", "test-pod", pod)
        assert structured["has_crash_loop"] is True
        assert "CrashLoopBackOff" in frag

    def test_oom_killed_detected(self):
        pod = _fake_pod(
            phase="Running",
            container_statuses=[{"name": "main", "terminated": {"reason": "OOMKilled", "exit_code": 137}}],
        )
        frag, structured = dkc._single_pod_status_fragment("ns", "test-pod", pod)
        assert structured["has_oom_killed"] is True

    def test_ready_false_detected(self):
        pod = _fake_pod(
            phase="Running",
            conditions=[{"type": "Ready", "status": "False"}],
        )
        frag, structured = dkc._single_pod_status_fragment("ns", "test-pod", pod)
        assert structured["ready_false"] is True

    def test_healthy_pod(self):
        pod = _fake_pod(phase="Running", conditions=[{"type": "Ready", "status": "True"}])
        frag, structured = dkc._single_pod_status_fragment("ns", "test-pod", pod)
        assert structured["has_crash_loop"] is False
        assert structured["has_oom_killed"] is False
        assert structured["ready_false"] is False


# ---------------------------------------------------------------------------
# _needs_previous_log
# ---------------------------------------------------------------------------

class TestNeedsPreviousLog:
    def test_crash_loop_needs_previous(self):
        pod = _fake_pod(container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}])
        assert dkc._needs_previous_log(pod) is True

    def test_terminated_with_restart_needs_previous(self):
        pod = _fake_pod(container_statuses=[{"name": "main", "terminated": {"reason": "Error"}, "restart_count": 2}])
        assert dkc._needs_previous_log(pod) is True

    def test_running_pod_no_previous(self):
        pod = _fake_pod(phase="Running")
        assert dkc._needs_previous_log(pod) is False

    def test_terminated_no_restart_no_previous(self):
        pod = _fake_pod(container_statuses=[{"name": "main", "terminated": {"reason": "Completed"}, "restart_count": 0}])
        assert dkc._needs_previous_log(pod) is False


# ---------------------------------------------------------------------------
# probe_k8s_clinical_pod_status — async (with mocked K8s)
# ---------------------------------------------------------------------------

class TestProbePodStatus:
    async def test_skipped_when_no_namespace(self):
        ev = AnomalyEvent(trace_id="trace-k01", canonical_query="{}", namespace="", gigo_metadata={})
        ctx = _make_ctx()
        result = await dkc.probe_k8s_clinical_pod_status(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_when_pod_found_with_deployment(self):
        ev = _make_ev(namespace="multi-agent", pod="my-svc-abc1234-xyzab")
        ctx = _make_ctx()

        pod_obj = _fake_pod(
            phase="Running",
            owner_references=[{"kind": "ReplicaSet", "name": "my-svc-rs", "controller": True}],
        )
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-abc1234-xyzab"}})])

        rs = _ns({"metadata": {"owner_references": [_ns({"kind": "Deployment", "name": "my-svc"})]}})
        dep = _ns({"spec": {"selector": {"match_labels": {"app": "my-svc"}}}})

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()
        apps_mock.read_namespaced_replica_set = AsyncMock(return_value=rs)
        apps_mock.read_namespaced_deployment = AsyncMock(return_value=dep)

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_status(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("kind") == "PodStatus"

    async def test_inconclusive_when_no_target_pods(self):
        ev = _make_ev()
        ctx = _make_ctx()

        from kubernetes_asyncio.client import ApiException
        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        # pod not found (404) → goes into _resolve_pods_when_pod_missing
        v1_mock.read_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))
        v1_mock.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[]))

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()
        apps_mock.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_status(ctx, ev)

        assert result.status in ("INCONCLUSIVE", "PASSED")

    async def test_failed_on_unexpected_exception(self):
        ev = _make_ev()
        ctx = _make_ctx()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", side_effect=RuntimeError("boom")),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=AsyncMock()),
        ):
            result = await dkc.probe_k8s_clinical_pod_status(ctx, ev)

        assert result.status == "FAILED"

    async def test_pod_only_no_controller(self):
        ev = _make_ev()
        ctx = _make_ctx()

        pod_obj = _fake_pod(phase="Running", owner_references=[])
        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_status(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("resolution") == "pod_only_no_controller"


# ---------------------------------------------------------------------------
# probe_k8s_clinical_pod_metrics — async
# ---------------------------------------------------------------------------

class TestProbePodMetrics:
    async def test_skipped_when_missing_pod(self):
        ev = AnomalyEvent(trace_id="trace-k02", canonical_query="{}", namespace="", gigo_metadata={})
        ctx = _make_ctx()
        result = await dkc.probe_k8s_clinical_pod_metrics(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_metrics_data(self):
        ev = _make_ev()
        ctx = _make_ctx()

        pod_obj = _fake_pod(phase="Running", owner_references=[])
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])
        metrics_obj = {
            "containers": [{"name": "main", "usage": {"cpu": "100m", "memory": "256Mi"}}]
        }

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        custom_mock = AsyncMock()
        custom_mock.api_client = _fake_api_client()
        custom_mock.get_namespaced_custom_object = AsyncMock(return_value=metrics_obj)

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
            patch("workers.diagnostic_k8s_clinical.client.CustomObjectsApi", return_value=custom_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_metrics(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("kind") == "PodMetrics"

    async def test_inconclusive_404_no_spec_limits(self):
        from kubernetes_asyncio.client import ApiException
        ev = _make_ev()
        ctx = _make_ctx()

        pod_obj = _fake_pod(phase="Running", owner_references=[], containers=[])
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        custom_mock = AsyncMock()
        custom_mock.api_client = _fake_api_client()
        custom_mock.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404))

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
            patch("workers.diagnostic_k8s_clinical.client.CustomObjectsApi", return_value=custom_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_metrics(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_spec_limits_fallback_when_no_metrics(self):
        from kubernetes_asyncio.client import ApiException
        ev = _make_ev()
        ctx = _make_ctx()

        pod_obj = _fake_pod(
            phase="Running",
            owner_references=[],
            containers=[{"name": "main", "resources": {"limits": {"memory": "512Mi"}}}],
        )
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        custom_mock = AsyncMock()
        custom_mock.api_client = _fake_api_client()
        custom_mock.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404))

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
            patch("workers.diagnostic_k8s_clinical.client.CustomObjectsApi", return_value=custom_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_metrics(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("kind") == "PodMetricsSpecFallback"

    async def test_failed_on_non_404_exception(self):
        from kubernetes_asyncio.client import ApiException
        ev = _make_ev()
        ctx = _make_ctx()

        pod_obj = _fake_pod(phase="Running", owner_references=[])
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        custom_mock = AsyncMock()
        custom_mock.api_client = _fake_api_client()
        custom_mock.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=500))

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
            patch("workers.diagnostic_k8s_clinical.client.CustomObjectsApi", return_value=custom_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_metrics(ctx, ev)

        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_k8s_clinical_pod_log_tail — async
# ---------------------------------------------------------------------------

class TestProbePodLogTail:
    async def test_skipped_when_no_namespace(self):
        ev = AnomalyEvent(trace_id="trace-k03", canonical_query="{}", namespace="", gigo_metadata={})
        ctx = _make_ctx()
        result = await dkc.probe_k8s_clinical_pod_log_tail(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_skipped_when_all_pods_healthy(self):
        ev = _make_ev()
        ctx = _make_ctx()

        healthy_pod = _fake_pod(phase="Running", conditions=[{"type": "Ready", "status": "True"}])
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=healthy_pod)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_tail(ctx, ev)

        assert result.status == "SKIPPED"
        assert "healthy" in result.raw_text.lower()

    async def test_passed_with_log_for_crashloop_pod(self):
        ev = _make_ev()
        ctx = _make_ctx()

        crashloop_pod = _fake_pod(
            phase="Running",
            containers=[{"name": "main"}],
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}}],
        )
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=crashloop_pod)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)
        v1_mock.read_namespaced_pod_log = AsyncMock(return_value="ERROR: connection refused\nPanic: nil pointer")

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_tail(ctx, ev)

        assert result.status == "PASSED"
        assert "CrashLoopBackOff" in result.raw_text or "container" in result.raw_text

    async def test_pending_imagepullbackoff_uses_events(self):
        ev = _make_ev()
        ctx = _make_ctx()

        pending_pod = _fake_pod(
            phase="Pending",
            containers=[{"name": "main"}],
            container_statuses=[{"name": "main", "waiting": {"reason": "ImagePullBackOff"}}],
        )
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])
        ev_items = [_ns({"type": "Warning", "reason": "Failed", "message": "pull fail", "last_timestamp": None, "event_time": None})]

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pending_pod)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)
        v1_mock.list_namespaced_event = AsyncMock(return_value=SimpleNamespace(items=ev_items))

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_tail(ctx, ev)

        assert result.status == "PASSED"
        assert "ImagePullBackOff" in result.raw_text or "pull" in result.raw_text.lower()

    async def test_failed_on_exception(self):
        ev = _make_ev()
        ctx = _make_ctx()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", side_effect=RuntimeError("oops")),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=AsyncMock()),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_tail(ctx, ev)

        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_k8s_clinical_pod_events — async
# ---------------------------------------------------------------------------

class TestProbePodEvents:
    async def test_skipped_when_no_namespace(self):
        ev = AnomalyEvent(trace_id="trace-k04", canonical_query="{}", namespace="", gigo_metadata={})
        ctx = _make_ctx()
        result = await dkc.probe_k8s_clinical_pod_events(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_events(self):
        ev = _make_ev()
        ctx = _make_ctx()

        pod_obj = _fake_pod(phase="Running", owner_references=[])
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])
        ev_items = [_ns({"type": "Normal", "reason": "Pulled", "message": "image pulled", "last_timestamp": None, "event_time": None})]

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=pod_obj)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)
        v1_mock.list_namespaced_event = AsyncMock(return_value=SimpleNamespace(items=ev_items))

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_events(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("kind") == "PodEvents"

    async def test_failed_on_exception(self):
        ev = _make_ev()
        ctx = _make_ctx()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", side_effect=RuntimeError("err")),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=AsyncMock()),
        ):
            result = await dkc.probe_k8s_clinical_pod_events(ctx, ev)

        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_k8s_resource_quota_probe — async
# ---------------------------------------------------------------------------

class TestProbeResourceQuota:
    async def test_skipped_when_no_namespace(self):
        ev = AnomalyEvent(trace_id="trace-k05", canonical_query="{}", namespace="", gigo_metadata={})
        ctx = _make_ctx()
        result = await dkc.probe_k8s_resource_quota_probe(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_quotas(self):
        ev = _make_ev()
        ctx = _make_ctx()

        rq_item = _ns({
            "metadata": {"name": "default-quota"},
            "status": {"hard": {"cpu": "4"}, "used": {"cpu": "1"}},
        })
        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.list_namespaced_resource_quota = AsyncMock(return_value=SimpleNamespace(items=[rq_item]))

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dkc.probe_k8s_resource_quota_probe(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("count") == 1

    async def test_passed_no_quotas(self):
        ev = _make_ev()
        ctx = _make_ctx()

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.list_namespaced_resource_quota = AsyncMock(return_value=SimpleNamespace(items=[]))

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dkc.probe_k8s_resource_quota_probe(ctx, ev)

        assert result.status == "PASSED"
        assert "no ResourceQuota" in result.raw_text

    async def test_failed_on_exception(self):
        ev = _make_ev()
        ctx = _make_ctx()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", side_effect=RuntimeError("kube error")),
        ):
            result = await dkc.probe_k8s_resource_quota_probe(ctx, ev)

        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_k8s_clinical_pod_log_previous — async
# ---------------------------------------------------------------------------

class TestProbePodLogPrevious:
    async def test_skipped_when_no_namespace(self):
        ev = AnomalyEvent(trace_id="trace-k06", canonical_query="{}", namespace="", gigo_metadata={})
        ctx = _make_ctx()
        result = await dkc.probe_k8s_clinical_pod_log_previous(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_skipped_when_no_crash_loop(self):
        ev = _make_ev()
        ctx = _make_ctx()

        healthy_pod = _fake_pod(phase="Running", containers=[{"name": "main"}])
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=healthy_pod)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_previous(ctx, ev)

        assert result.status == "SKIPPED"
        assert "CrashLoopBackOff" in result.raw_text or "previous log" in result.raw_text.lower()

    async def test_passed_with_crash_loop_previous_log(self):
        ev = _make_ev()
        ctx = _make_ctx()

        crash_pod = _fake_pod(
            phase="Running",
            containers=[{"name": "main"}],
            container_statuses=[{"name": "main", "waiting": {"reason": "CrashLoopBackOff"}, "restart_count": 3}],
        )
        pod_list = SimpleNamespace(items=[_ns({"metadata": {"name": "my-svc-pod"}})])

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(return_value=crash_pod)
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)
        v1_mock.read_namespaced_pod_log = AsyncMock(return_value="FATAL: OOM at startup")

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_previous(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint.get("k8s_log_previous") is True

    async def test_failed_on_exception(self):
        ev = _make_ev()
        ctx = _make_ctx()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", side_effect=RuntimeError("err")),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=AsyncMock()),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_previous(ctx, ev)

        assert result.status == "FAILED"

    async def test_inconclusive_when_no_target_pods(self):
        from kubernetes_asyncio.client import ApiException
        ev = _make_ev()
        ctx = _make_ctx()

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))
        v1_mock.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[]))

        apps_mock = AsyncMock()
        apps_mock.api_client = _fake_api_client()
        apps_mock.read_namespaced_deployment = AsyncMock(side_effect=ApiException(status=404))

        batch_mock = AsyncMock()
        batch_mock.api_client = _fake_api_client()

        with (
            patch("workers.diagnostic_k8s_clinical._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=v1_mock),
            patch("workers.diagnostic_k8s_clinical.client.AppsV1Api", return_value=apps_mock),
            patch("workers.diagnostic_k8s_clinical.client.BatchV1Api", return_value=batch_mock),
        ):
            result = await dkc.probe_k8s_clinical_pod_log_previous(ctx, ev)

        assert result.status == "INCONCLUSIVE"
