"""Wave W3: pure helpers in k8s_tools, diagnostic_probe_registry, sdk_service_tools (mocked IO)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from kubernetes_asyncio.client import ApiException


# ── k8s_tools ───────────────────────────────────────────────────────────────


def test_redis_key_helpers():
    from workers import k8s_tools as kt

    assert kt.redis_key_rollout_pending(12345) == "omni:rollout_pending:12345"
    assert kt.redis_key_write_pending(99) == "omni:write_pending:99"


def test_discover_pairs_from_hint_ordering():
    from workers import k8s_tools as kt

    pairs = [
        ("ns", "other-pod"),
        ("ns", "nginx-long-name"),
        ("x", "nginx-ab"),
        ("x", "nginx-abc"),
    ]
    pref_all = kt._discover_pairs_from_hint("nginx", pairs)
    assert pref_all == sorted(
        [("ns", "nginx-long-name"), ("x", "nginx-ab"), ("x", "nginx-abc")],
        key=lambda x: len(x[1]),
    )
    exact = kt._discover_pairs_from_hint("nginx-abc", pairs)
    assert exact == [("x", "nginx-abc")]
    sub = kt._discover_pairs_from_hint("nginx-a", pairs)
    assert sub[0] == ("x", "nginx-ab")


def test_cpu_mem_pct_usage_helpers():
    from workers import k8s_tools as kt

    assert kt._cpu_to_cores("100m") == pytest.approx(0.1)
    assert kt._cpu_to_cores("500000n") == pytest.approx(0.0005)
    assert kt._cpu_to_cores("2") == 2.0
    assert kt._cpu_to_cores(None) == 0.0

    assert kt._mem_to_bytes("512Mi") == 512 * 1024**2
    assert kt._mem_to_bytes("1Gi") == 1024**3
    assert kt._mem_to_bytes("100") == 100  # digits only

    assert kt._pct(50.0, 100.0) == 50.0
    assert kt._pct(1.0, 0.0) == 0.0
    assert kt._pct(200.0, 100.0) == 100.0  # capped

    body = {
        "containers": [
            {"usage": {"cpu": "50m", "memory": "128Mi"}},
            {"usage": {"cpu": "100m", "memory": "256Mi"}},
        ]
    }
    c, m = kt._usage_from_metrics_body(body)
    assert c == pytest.approx(0.15)
    assert m == 128 * 1024**2 + 256 * 1024**2


def test_resolve_pod_name_and_format_pod_list():
    from workers import k8s_tools as kt

    class MD:
        def __init__(self, name: str):
            self.name = name

    class P:
        def __init__(self, name: str, phase: str = "Running", ip: str = "10.0.0.1"):
            self.metadata = MD(name)
            self.status = SimpleNamespace(phase=phase, pod_ip=ip)

    items = [P("zebra"), P("nginx-aaa"), P("nginx-abcdef")]
    assert kt._resolve_pod_name("nginx", items) == "nginx-aaa"
    assert kt._resolve_pod_name("missing", items) is None

    resp = SimpleNamespace(items=[P("b", "Pending", "-"), P("a")])
    text = kt._format_pod_list(resp, "ns1")
    assert "Pods namespace `ns1`" in text
    assert "a\tRunning" in text


def test_parse_kubectl_get_pods_lines():
    from workers import k8s_tools as kt

    lines = [
        "NAMESPACE   NAME    READY\n",
        "default     pod-a   1/1\n",
        "kube-sys    coredns 1/1\n",
        "\n",
    ]
    pairs, body = kt._parse_kubectl_get_pods_lines(lines)
    assert pairs == [("default", "pod-a"), ("kube-sys", "coredns")]
    assert len(body) == 2


def test_event_is_warning_or_critical():
    from workers import k8s_tools as kt

    assert kt._event_is_warning_or_critical(SimpleNamespace(type="Warning", reason="x"))
    assert kt._event_is_warning_or_critical(SimpleNamespace(type="Normal", reason="OOMKilled"))
    assert not kt._event_is_warning_or_critical(SimpleNamespace(type="Normal", reason="Scheduled"))


def test_ws_allows_kubectl_list_all():
    from workers import k8s_tools as kt

    assert kt._ws_allows_kubectl_list_all(SimpleNamespace(settings=None)) is False
    assert kt._ws_allows_kubectl_list_all(SimpleNamespace(settings=SimpleNamespace(lab_unchained=False, god_mode=False))) is False
    assert kt._ws_allows_kubectl_list_all(SimpleNamespace(settings=SimpleNamespace(lab_unchained=True, god_mode=False))) is True


def test_aggregate_limits_prefers_limits_then_requests():
    from workers import k8s_tools as kt

    class R:
        def __init__(self, lim: dict | None = None, req: dict | None = None):
            self.limits = lim or {}
            self.requests = req or {}

    class C:
        def __init__(self, lim: dict | None = None, req: dict | None = None):
            self.resources = R(lim=lim, req=req)

    pod = SimpleNamespace(spec=SimpleNamespace(containers=[C(lim={"cpu": "200m", "memory": "64Mi"})]))
    cpu, mem = kt._aggregate_limits(pod)
    assert cpu == pytest.approx(0.2)
    assert mem == 64 * 1024**2

    pod2 = SimpleNamespace(
        spec=SimpleNamespace(
            containers=[C(lim={}, req={"cpu": "100m", "memory": "32Mi"})],
        )
    )
    cpu2, mem2 = kt._aggregate_limits(pod2)
    assert cpu2 == pytest.approx(0.1)
    assert mem2 == 32 * 1024**2


@pytest.mark.asyncio
async def test_resolve_pod_identity_empty_hint():
    from workers import k8s_tools as kt

    v1 = AsyncMock()
    ident = await kt.resolve_pod_identity(v1, "", None)
    assert ident.kind == "not_found_cluster"
    v1.list_pod_for_all_namespaces.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_pod_identity_explicit_ns_not_found():
    from workers import k8s_tools as kt

    v1 = AsyncMock()
    v1.list_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))
    ident = await kt.resolve_pod_identity(v1, "nginx", "missing-ns")
    assert ident.kind == "not_found_namespace"
    assert ident.namespace == "missing-ns"


@pytest.mark.asyncio
async def test_discover_pod_across_namespaces_mocked():
    from workers import k8s_tools as kt

    class MD:
        def __init__(self, ns: str, name: str):
            self.namespace = ns
            self.name = name

    class Pod:
        def __init__(self, ns: str, name: str):
            self.metadata = MD(ns, name)

    v1 = AsyncMock()
    v1.list_pod_for_all_namespaces = AsyncMock(
        return_value=SimpleNamespace(items=[Pod("a", "redis-1"), Pod("b", "nginx-xyz")]),
    )
    out = await kt.discover_pod_across_namespaces(v1, "nginx")
    assert out == [("b", "nginx-xyz")]


@pytest.mark.asyncio
async def test_resolve_deployment_identity_ambiguous():
    from workers import k8s_tools as kt

    class MD:
        def __init__(self, ns: str, name: str):
            self.namespace = ns
            self.name = name

    class Dep:
        def __init__(self, ns: str, name: str):
            self.metadata = MD(ns, name)

    apps = AsyncMock()
    apps.list_deployment_for_all_namespaces = AsyncMock(
        return_value=SimpleNamespace(items=[Dep("n1", "api"), Dep("n2", "api")]),
    )
    ident = await kt.resolve_deployment_identity(apps, "api", None)
    assert ident.kind == "ambiguous"
    assert len(ident.candidates) == 2


# ── diagnostic_probe_registry ───────────────────────────────────────────────


def test_prom_label_esc():
    from workers.diagnostic_probe_registry import _prom_label_esc

    assert _prom_label_esc('a"b\\c') == 'a\\"b\\\\c'


def test_instant_vector_summary_paths():
    from workers.diagnostic_probe_registry import _instant_vector_summary

    s, st = _instant_vector_summary({"status": "error"})
    assert "prom_status=" in s
    assert st == {}

    s2, st2 = _instant_vector_summary({"status": "success", "data": {"result": []}})
    assert s2 == "empty_vector"
    assert st2 == {}

    data = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"pod": "p1", "container": "c1"}, "value": [1.0, "0.5"]},
                {"metric": {}, "value": [1.0, "not-a-float"]},
            ],
        },
    }
    s3, st3 = _instant_vector_summary(data)
    assert "p1/c1=0.5" in s3
    assert st3["p1/c1"] == 0.5
    assert "s1=None" in s3 or "s1=" in s3


@pytest.mark.asyncio
async def test_prometheus_instant_query_missing_url():
    from workers.diagnostic_probe_registry import _prometheus_instant_query
    from workers.handler_context import WorkerHandlerContext
    from workers.proactive_models import AnomalyEvent

    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = SimpleNamespace(prometheus_url="")
    ev = AnomalyEvent(trace_id="abcd", canonical_query="{}")
    out = await _prometheus_instant_query(ctx, "up")
    assert out["status"] == "error"
    assert "unset" in out.get("error", "")


@pytest.mark.asyncio
async def test_probe_node_disk_pressure_empty_result():
    from workers import diagnostic_probe_registry as dpr
    from workers.handler_context import WorkerHandlerContext
    from workers.proactive_models import AnomalyEvent

    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = SimpleNamespace(prometheus_url="http://prom:9090")
    ev = AnomalyEvent(trace_id="abcd", canonical_query="{}")
    with patch.object(
        dpr,
        "_prometheus_instant_query",
        new=AsyncMock(return_value={"status": "success", "data": {"result": []}}),
    ):
        raw = await dpr.probe_node_disk_pressure(ctx, ev)
    assert raw.status == "PASSED"


@pytest.mark.asyncio
async def test_probe_prom_pod_cpu_skipped_without_workload():
    from workers import diagnostic_probe_registry as dpr
    from workers.handler_context import WorkerHandlerContext
    from workers.proactive_models import AnomalyEvent

    ctx = MagicMock(spec=WorkerHandlerContext)
    ev = AnomalyEvent(trace_id="abcd", canonical_query="{}", namespace="", deployment="")
    raw = await dpr.probe_prom_pod_cpu_cores(ctx, ev)
    assert raw.status == "SKIPPED"


@pytest.mark.asyncio
async def test_probe_node_cpu_saturation_connect_error():
    from workers import diagnostic_probe_registry as dpr
    from workers.handler_context import WorkerHandlerContext
    from workers.proactive_models import AnomalyEvent

    ctx = MagicMock(spec=WorkerHandlerContext)
    ev = AnomalyEvent(trace_id="abcd", canonical_query="{}")
    with patch.object(
        dpr,
        "_prometheus_instant_query",
        new=AsyncMock(side_effect=httpx.ConnectError("refused")),
    ):
        raw = await dpr.probe_node_cpu_saturation(ctx, ev)
    assert raw.status == "INCONCLUSIVE"


@pytest.mark.asyncio
async def test_run_probe_unknown_id():
    from workers import diagnostic_probe_registry as dpr
    from workers.handler_context import WorkerHandlerContext
    from workers.proactive_models import AnomalyEvent

    ctx = MagicMock(spec=WorkerHandlerContext)
    ev = AnomalyEvent(trace_id="abcd", canonical_query="{}")
    raw = await dpr.run_probe("no_such_probe", ctx, ev)
    assert raw.status == "SKIPPED"
    assert "unknown" in raw.raw_text.lower()


# ── sdk_service_tools (pure / no K8s) ─────────────────────────────────────────


def test_is_placeholder_promql():
    from workers import sdk_service_tools as sst

    assert sst.is_placeholder_promql("")
    assert sst.is_placeholder_promql("   ")
    assert sst.is_placeholder_promql("metric_value > threshold")
    assert sst.is_placeholder_promql("METRIC_VALUE>=THRESHOLD")
    assert not sst.is_placeholder_promql("up == 1")


def test_duration_window_label_and_vm_window():
    from workers import sdk_service_tools as sst

    assert "giờ" in sst._duration_window_label("6h")
    assert "phút" in sst._duration_window_label("15m")
    assert sst._duration_window_label("") == "1 giờ"

    assert sst._duration_to_vm_window("1h") == ("now-1h", "30s")
    assert sst._duration_to_vm_window("24h") == ("now-24h", "5m")
    assert sst._duration_to_vm_window("30m") == ("now-30m", "15s")
    assert sst._duration_to_vm_window("weird") == ("now-1h", "30s")


def test_prometheus_base_url_and_default_namespace():
    from workers import sdk_service_tools as sst

    class S:
        prometheus_url = " https://prom.example/prometheus/ "
        k8s_default_namespace = " prod-ns "

    ctx = SimpleNamespace(settings=S())
    assert sst._prometheus_base_url(ctx) == "https://prom.example/prometheus"
    assert sst._default_namespace(ctx) == "prod-ns"
    assert sst._default_namespace(SimpleNamespace(settings=SimpleNamespace(k8s_default_namespace=""))) == "multi-agent"


def test_vm_user_facing_errors():
    from workers import sdk_service_tools as sst

    class Forbidden(sst.VMHTTPForbidden):
        pass

    msg = sst._vm_user_facing_error(Forbidden())
    assert "khong_co_quyen" in msg

    req = httpx.Request("GET", "http://x")
    resp = httpx.Response(403, request=req)
    err = httpx.HTTPStatusError("403", request=req, response=resp)
    assert "khong_co_quyen" in sst._vm_user_facing_error(err)

    conn = httpx.ConnectError("nope", request=req)
    assert "OMNI_PROMETHEUS_URL" in sst._vm_user_facing_error(conn)


def test_diagnosis_vm_empty_variants():
    from workers import sdk_service_tools as sst

    assert "Host/node" in sst._diagnosis_vm_empty({"target_type": "host"}, "1h", promql="up")
    assert "kube-state-metrics" in sst._diagnosis_vm_empty(
        {"target_type": "kube_deployment", "namespace": "ns", "deployment": "d"},
        "2h",
        promql="x",
    )
    assert "kube-state-metrics" in sst._diagnosis_vm_empty(
        {"target_type": "kube_namespace", "namespace": "ns"},
        "1h",
        promql="y",
    )
    assert "namespace=\"ns\" pod=\"p\"" in sst._diagnosis_vm_empty(
        {"namespace": "ns", "pod_name": "p"},
        "1h",
        promql="z",
    )


def test_fmt_slowlog_entry():
    from workers import sdk_service_tools as sst

    assert "dur_ms=12" in sst._fmt_slowlog_entry({"id": 1, "duration": 12, "command": b"GET x"})
    class E:
        id = 2
        duration = 5
        command = b"PING"

    assert "id=2" in sst._fmt_slowlog_entry(E())
    assert sst._fmt_slowlog_entry("raw") == "'raw'"


@pytest.mark.asyncio
async def test_resolve_promql_explicit_query():
    from workers import sdk_service_tools as sst

    ctx = SimpleNamespace(settings=SimpleNamespace(k8s_default_namespace="multi-agent"))
    q, note = sst.resolve_promql_for_args({"query": "  up  ", "namespace": "x"}, ctx)
    assert q == "up"
    assert note == "explicit_query"


@pytest.mark.asyncio
async def test_vm_instant_scalar_paths():
    from workers import sdk_service_tools as sst

    ctx = object()
    with patch.object(sst, "_prometheus_get_json", new=AsyncMock(side_effect=RuntimeError("net"))):
        assert await sst._vm_instant_scalar(ctx, "up") is None

    with patch.object(
        sst,
        "_prometheus_get_json",
        new=AsyncMock(return_value={"status": "success", "data": {"result": [{"value": [1.0, "3.14"]}]}}),
    ):
        assert await sst._vm_instant_scalar(ctx, "up") == pytest.approx(3.14)

    with patch.object(
        sst,
        "_prometheus_get_json",
        new=AsyncMock(return_value={"status": "success", "data": {"result": []}}),
    ):
        assert await sst._vm_instant_scalar(ctx, "up") is None
