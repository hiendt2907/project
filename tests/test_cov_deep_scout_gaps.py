"""Coverage-gap tests for src/init/deep_scout.py (26.4% covered → raise to ~70%).

Tests focus on:
- Pure helpers: _is_sensitive_config_key, _redact_configmap_entries,
  _embedding_from_response, _point_id_stable
- DeepScoutSummary dataclass
- _layer_host_node (psutil + k8s)
- _layer_network_topology (k8s)
- _layer_metrics_baseline (httpx → Prometheus)
- _layer_cluster_state (pods, PVs, ConfigMaps)
- run_deep_scout (wires layers together)
- deep_scout_periodic_loop
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OMNI_PROMETHEUS_URL", "http://prometheus:9090")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

from init.deep_scout import (
    _is_sensitive_config_key,
    _redact_configmap_entries,
    _embedding_from_response,
    _point_id_stable,
    DeepScoutSummary,
)


class TestIsSensitiveConfigKey:
    def test_kubernetes_prefix(self):
        assert _is_sensitive_config_key("kubernetes.io/something") is True

    def test_password_in_key(self):
        assert _is_sensitive_config_key("db_password") is True

    def test_token(self):
        assert _is_sensitive_config_key("api_token") is True

    def test_secret(self):
        assert _is_sensitive_config_key("my_secret_key") is True

    def test_apikey(self):
        assert _is_sensitive_config_key("apikey") is True

    def test_api_key(self):
        assert _is_sensitive_config_key("API_KEY") is True

    def test_auth(self):
        assert _is_sensitive_config_key("auth_header") is True

    def test_bearer(self):
        assert _is_sensitive_config_key("Bearer") is True

    def test_access_key(self):
        assert _is_sensitive_config_key("access_key_id") is True

    def test_client_secret(self):
        assert _is_sensitive_config_key("client_secret") is True

    def test_safe_key(self):
        assert _is_sensitive_config_key("database_host") is False

    def test_safe_key_port(self):
        assert _is_sensitive_config_key("PORT") is False

    def test_empty_key(self):
        assert _is_sensitive_config_key("") is False

    def test_credential_key(self):
        assert _is_sensitive_config_key("credential_file") is True

    def test_privatekey(self):
        assert _is_sensitive_config_key("privatekey_pem") is True


class TestRedactConfigmapEntries:
    def test_sensitive_key_redacted(self):
        data = {"db_password": "secret123", "host": "localhost"}
        result = _redact_configmap_entries(data)
        assert result["db_password"] == "<REDACTED>"
        assert result["host"] == "localhost"

    def test_value_truncated_at_500(self):
        data = {"config": "x" * 1000}
        result = _redact_configmap_entries(data)
        assert len(result["config"]) == 500

    def test_none_value_handled(self):
        data = {"key": None}
        result = _redact_configmap_entries(data)
        assert result["key"] == ""

    def test_empty_data(self):
        assert _redact_configmap_entries({}) == {}

    def test_multiple_sensitive_keys(self):
        data = {"api_token": "tok", "passwd": "pw", "url": "http://x"}
        result = _redact_configmap_entries(data)
        assert result["api_token"] == "<REDACTED>"
        assert result["passwd"] == "<REDACTED>"
        assert result["url"] == "http://x"


class TestEmbeddingFromResponse:
    def test_direct_embedding(self):
        resp = {"embedding": [0.1, 0.2, 0.3]}
        assert _embedding_from_response(resp) == [0.1, 0.2, 0.3]

    def test_embedding_not_list_converted(self):
        resp = {"embedding": (0.1, 0.2)}
        result = _embedding_from_response(resp)
        assert result == [0.1, 0.2]

    def test_embeddings_plural_first(self):
        resp = {"embeddings": [[0.5, 0.6], [0.7, 0.8]]}
        result = _embedding_from_response(resp)
        assert result == [0.5, 0.6]

    def test_missing_raises(self):
        with pytest.raises(ValueError, match="embedding"):
            _embedding_from_response({})

    def test_empty_embeddings_raises(self):
        with pytest.raises(ValueError):
            _embedding_from_response({"embeddings": []})


class TestPointIdStable:
    def test_deterministic(self):
        a = _point_id_stable("host_node")
        b = _point_id_stable("host_node")
        assert a == b

    def test_different_strings_different_ids(self):
        a = _point_id_stable("host_node")
        b = _point_id_stable("network_topology")
        assert a != b

    def test_valid_uuid_format(self):
        import uuid
        s = _point_id_stable("cluster_state")
        # Should be parseable as UUID
        uuid.UUID(s)


class TestDeepScoutSummary:
    def test_defaults(self):
        s = DeepScoutSummary()
        assert s.n_nodes == 0
        assert s.n_pods == 0
        assert s.n_services == 0
        assert s.vm_url == ""
        assert s.errors == []

    def test_custom(self):
        s = DeepScoutSummary(n_nodes=3, n_pods=12, vm_url="http://prom:9090")
        assert s.n_nodes == 3
        assert s.n_pods == 12


# ---------------------------------------------------------------------------
# _layer_host_node
# ---------------------------------------------------------------------------

from init.deep_scout import _layer_host_node


@pytest.mark.asyncio
async def test_layer_host_node_psutil_success(monkeypatch: pytest.MonkeyPatch):
    """psutil works, k8s fails gracefully."""
    fake_vm = MagicMock()
    fake_vm.total = 8 * (1024 ** 3)
    fake_vm.percent = 45.0

    fake_disk = MagicMock()
    fake_disk.percent = 60.0

    fake_io = MagicMock()
    fake_io.read_bytes = 1000
    fake_io.write_bytes = 2000

    import psutil
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4 if logical else 2)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: fake_vm)
    monkeypatch.setattr(psutil, "disk_usage", lambda path: fake_disk)
    monkeypatch.setattr(psutil, "disk_io_counters", lambda: fake_io)

    async def fake_kube_load():
        raise Exception("k8s not available")

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)

    ws = MagicMock()
    ws.prometheus_url = "http://prom:9090"
    ws.deep_scout_configmap_namespaces = "multi-agent"

    host, txt = await _layer_host_node(ws)
    assert host["cpu_count_logical"] == 4
    assert host["ram_total_gib"] > 0
    assert "k8s_node_error" in host


@pytest.mark.asyncio
async def test_layer_host_node_psutil_and_kube_success(monkeypatch: pytest.MonkeyPatch):
    """Both psutil and k8s succeed."""
    fake_vm = MagicMock()
    fake_vm.total = 16 * (1024 ** 3)
    fake_vm.percent = 30.0

    fake_disk = MagicMock()
    fake_disk.percent = 40.0

    import psutil
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 8 if logical else 4)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: fake_vm)
    monkeypatch.setattr(psutil, "disk_usage", lambda path: fake_disk)
    monkeypatch.setattr(psutil, "disk_io_counters", lambda: None)  # io=None branch

    async def fake_kube_load():
        pass

    node = MagicMock()
    node.metadata.name = "node-1"
    node.status.capacity = {"cpu": "4", "memory": "8Gi"}

    fake_v1 = AsyncMock()
    fake_v1.list_node = AsyncMock(return_value=MagicMock(items=[node]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("init.deep_scout.client.CoreV1Api", lambda: fake_v1)

    ws = MagicMock()
    ws.prometheus_url = "http://prom:9090"

    host, txt = await _layer_host_node(ws)
    assert len(host["nodes"]) == 1
    assert host["nodes"][0]["name"] == "node-1"


@pytest.mark.asyncio
async def test_layer_host_node_psutil_error(monkeypatch: pytest.MonkeyPatch):
    """psutil raises → graceful error."""
    import psutil
    monkeypatch.setattr(psutil, "cpu_count", MagicMock(side_effect=RuntimeError("no psutil")))

    async def fake_kube_load():
        raise Exception("no k8s")

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)

    ws = MagicMock()
    host, txt = await _layer_host_node(ws)
    assert "psutil_error" in host


# ---------------------------------------------------------------------------
# _layer_network_topology
# ---------------------------------------------------------------------------

from init.deep_scout import _layer_network_topology


@pytest.mark.asyncio
async def test_layer_network_topology_success(monkeypatch: pytest.MonkeyPatch):
    async def fake_kube_load():
        pass

    svc = MagicMock()
    svc.metadata.namespace = "default"
    svc.metadata.name = "my-svc"
    svc.spec.type = "ClusterIP"
    svc.spec.cluster_ip = "10.0.0.1"
    port = MagicMock()
    port.port = 80
    port.protocol = "TCP"
    port.name = "http"
    svc.spec.ports = [port]

    ep = MagicMock()
    ep.metadata.namespace = "default"
    ep.metadata.name = "my-svc"
    addr = MagicMock()
    addr.ip = "172.17.0.5"
    addr.target_ref = MagicMock()
    addr.target_ref.kind = "Pod"
    addr.target_ref.name = "my-pod"
    subset = MagicMock()
    subset.addresses = [addr]
    ep.subsets = [subset]

    ing_item = MagicMock()
    ing_item.metadata.namespace = "default"
    ing_item.metadata.name = "my-ing"
    rule = MagicMock()
    rule.host = "example.com"
    path_item = MagicMock()
    path_item.path = "/"
    rule.http.paths = [path_item]
    ing_item.spec.rules = [rule]

    fake_v1 = AsyncMock()
    fake_v1.list_service_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[svc]))
    fake_v1.list_endpoints_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[ep]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    fake_net = AsyncMock()
    fake_net.list_ingress_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[ing_item]))
    fake_net.api_client = MagicMock()
    fake_net.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("init.deep_scout.client.CoreV1Api", lambda: fake_v1)
    monkeypatch.setattr("init.deep_scout.client.NetworkingV1Api", lambda: fake_net)

    topo, txt = await _layer_network_topology()
    assert len(topo["services"]) == 1
    assert topo["services"][0]["name"] == "my-svc"
    assert "ingress" in topo


@pytest.mark.asyncio
async def test_layer_network_topology_kube_error(monkeypatch: pytest.MonkeyPatch):
    async def fake_kube_load():
        raise RuntimeError("no cluster")

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)

    topo, txt = await _layer_network_topology()
    assert "error" in topo


@pytest.mark.asyncio
async def test_layer_network_topology_ingress_error(monkeypatch: pytest.MonkeyPatch):
    """Ingress list fails → topo has ingress_error, services still returned."""
    async def fake_kube_load():
        pass

    fake_v1 = AsyncMock()
    fake_v1.list_service_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    fake_v1.list_endpoints_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    fake_net = AsyncMock()
    fake_net.list_ingress_for_all_namespaces = AsyncMock(side_effect=RuntimeError("no ingress"))
    fake_net.api_client = MagicMock()
    fake_net.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("init.deep_scout.client.CoreV1Api", lambda: fake_v1)
    monkeypatch.setattr("init.deep_scout.client.NetworkingV1Api", lambda: fake_net)

    topo, txt = await _layer_network_topology()
    assert "ingress_error" in topo


# ---------------------------------------------------------------------------
# _layer_metrics_baseline
# ---------------------------------------------------------------------------

from init.deep_scout import _layer_metrics_baseline


@pytest.mark.asyncio
async def test_layer_metrics_baseline_success(monkeypatch: pytest.MonkeyPatch):
    import httpx

    async def fake_get(url, params=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = lambda: {
            "status": "success",
            "data": {"result": [{"value": [1, "42"]}]},
        }
        return resp

    fake_client = AsyncMock()
    fake_client.get = fake_get
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_client)

    ws = MagicMock()
    ws.prometheus_url = "http://prom:9090"

    met, txt = await _layer_metrics_baseline(ws)
    assert "queries" in met
    assert "count_up" in met["queries"]


@pytest.mark.asyncio
async def test_layer_metrics_baseline_prometheus_error(monkeypatch: pytest.MonkeyPatch):
    import httpx

    async def fake_get(url, params=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock()))
        return resp

    fake_client = AsyncMock()
    fake_client.get = fake_get
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_client)

    ws = MagicMock()
    ws.prometheus_url = "http://prom:9090"

    met, txt = await _layer_metrics_baseline(ws)
    assert "queries" in met
    for v in met["queries"].values():
        assert v is None or str(v).startswith("exc:")


@pytest.mark.asyncio
async def test_layer_metrics_baseline_connection_error(monkeypatch: pytest.MonkeyPatch):
    import httpx

    async def bad_get(url, params=None):
        raise httpx.ConnectError("dns fail")

    fake_client = AsyncMock()
    fake_client.get = bad_get
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_client)

    ws = MagicMock()
    ws.prometheus_url = "http://prom:9090"

    met, txt = await _layer_metrics_baseline(ws)
    # Should have error or fallback
    assert isinstance(met, dict)


# ---------------------------------------------------------------------------
# _layer_cluster_state
# ---------------------------------------------------------------------------

from init.deep_scout import _layer_cluster_state


@pytest.mark.asyncio
async def test_layer_cluster_state_success(monkeypatch: pytest.MonkeyPatch):
    async def fake_kube_load():
        pass

    pod = MagicMock()
    pod.metadata.namespace = "multi-agent"

    pv = MagicMock()
    pvc = MagicMock()

    cm = MagicMock()
    cm.metadata.name = "my-config"
    cm.data = {"host": "localhost", "db_password": "secret"}

    fake_v1 = AsyncMock()
    fake_v1.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[pod]))
    fake_v1.list_persistent_volume = AsyncMock(return_value=MagicMock(items=[pv]))
    fake_v1.list_persistent_volume_claim_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[pvc]))
    fake_v1.list_namespaced_config_map = AsyncMock(return_value=MagicMock(items=[cm]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("init.deep_scout.client.CoreV1Api", lambda: fake_v1)

    ws = MagicMock()
    ws.deep_scout_configmap_namespaces = "multi-agent"

    state, txt = await _layer_cluster_state(ws)
    assert state["pod_count"] == 1
    assert state["pv_count"] == 1
    assert state["pvc_count"] == 1
    assert len(state["configmaps_summary"]) == 1
    # password should be redacted
    assert state["configmaps_summary"][0]["redacted_values_preview"].get("db_password") == "<REDACTED>"


@pytest.mark.asyncio
async def test_layer_cluster_state_kube_error(monkeypatch: pytest.MonkeyPatch):
    async def fake_kube_load():
        raise RuntimeError("no cluster")

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)

    ws = MagicMock()
    ws.deep_scout_configmap_namespaces = "multi-agent"

    state, txt = await _layer_cluster_state(ws)
    assert "error" in state


@pytest.mark.asyncio
async def test_layer_cluster_state_pv_error(monkeypatch: pytest.MonkeyPatch):
    """PV list fails → pv_error in state, rest continues."""
    async def fake_kube_load():
        pass

    pod = MagicMock()
    pod.metadata.namespace = "ns"

    from kubernetes_asyncio.client import ApiException

    fake_v1 = AsyncMock()
    fake_v1.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[pod]))
    fake_v1.list_persistent_volume = AsyncMock(side_effect=RuntimeError("pv error"))
    fake_v1.list_persistent_volume_claim_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    fake_v1.list_namespaced_config_map = AsyncMock(return_value=MagicMock(items=[]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("init.deep_scout.client.CoreV1Api", lambda: fake_v1)

    ws = MagicMock()
    ws.deep_scout_configmap_namespaces = "multi-agent"

    state, txt = await _layer_cluster_state(ws)
    assert "pv_error" in state
    assert state["pod_count"] == 1


# ---------------------------------------------------------------------------
# run_deep_scout
# ---------------------------------------------------------------------------

from init.deep_scout import run_deep_scout


@pytest.mark.asyncio
async def test_run_deep_scout_success(monkeypatch: pytest.MonkeyPatch):
    """Full run_deep_scout with all layers mocked."""

    async def fake_layer_host(ws):
        return {"nodes": [{"name": "n1"}]}, "host ok"

    async def fake_layer_topo():
        return {"services": [{"name": "s1"}]}, "topo ok"

    async def fake_layer_met(ws):
        return {"queries": {"count_up": "3"}}, "met ok"

    async def fake_layer_cluster(ws):
        return {"pod_count": 5}, "cluster ok"

    async def fake_embed_upsert(llm, ws, vs, chunks, sem):
        pass

    monkeypatch.setattr("init.deep_scout._layer_host_node", fake_layer_host)
    monkeypatch.setattr("init.deep_scout._layer_network_topology", fake_layer_topo)
    monkeypatch.setattr("init.deep_scout._layer_metrics_baseline", fake_layer_met)
    monkeypatch.setattr("init.deep_scout._layer_cluster_state", fake_layer_cluster)
    monkeypatch.setattr("init.deep_scout._embed_and_upsert", fake_embed_upsert)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock()

    ws = MagicMock()
    ws.deep_scout_embed_concurrency = 2
    ws.prometheus_url = "http://prom:9090"
    ws.deep_scout_configmap_namespaces = "multi-agent"

    ctx = SimpleNamespace(
        settings=ws,
        redis=fake_redis,
        llm=AsyncMock(),
        vector_store=AsyncMock(),
    )

    summary = await run_deep_scout(ctx, periodic=False)
    assert summary.n_nodes == 1
    assert summary.n_pods == 5
    assert summary.n_services == 1
    assert fake_redis.set.call_count >= 2


@pytest.mark.asyncio
async def test_run_deep_scout_redis_error(monkeypatch: pytest.MonkeyPatch):
    """Redis write failure → error added to summary but function completes."""

    async def fake_layer_host(ws):
        return {"nodes": []}, ""

    async def fake_layer_topo():
        return {"services": []}, ""

    async def fake_layer_met(ws):
        return {"queries": {}}, ""

    async def fake_layer_cluster(ws):
        return {"pod_count": 0}, ""

    async def fake_embed_upsert(llm, ws, vs, chunks, sem):
        pass

    monkeypatch.setattr("init.deep_scout._layer_host_node", fake_layer_host)
    monkeypatch.setattr("init.deep_scout._layer_network_topology", fake_layer_topo)
    monkeypatch.setattr("init.deep_scout._layer_metrics_baseline", fake_layer_met)
    monkeypatch.setattr("init.deep_scout._layer_cluster_state", fake_layer_cluster)
    monkeypatch.setattr("init.deep_scout._embed_and_upsert", fake_embed_upsert)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(side_effect=RuntimeError("redis down"))

    ws = MagicMock()
    ws.deep_scout_embed_concurrency = 2
    ws.prometheus_url = "http://prom:9090"

    ctx = SimpleNamespace(
        settings=ws,
        redis=fake_redis,
        llm=AsyncMock(),
        vector_store=AsyncMock(),
    )

    summary = await run_deep_scout(ctx)
    assert len(summary.errors) > 0
    assert any("redis" in e for e in summary.errors)


# ---------------------------------------------------------------------------
# deep_scout_periodic_loop
# ---------------------------------------------------------------------------

from init.deep_scout import deep_scout_periodic_loop


@pytest.mark.asyncio
async def test_deep_scout_periodic_loop_immediate_stop(monkeypatch: pytest.MonkeyPatch):
    """Stop event set before timeout → loop exits without running scout."""
    called = []

    async def fake_run(ctx, *, periodic=False):
        called.append(1)
        return MagicMock()

    monkeypatch.setattr("init.deep_scout.run_deep_scout", fake_run)

    ws = MagicMock()
    ws.deep_scout_interval_sec = 999  # won't fire before stop

    stop = asyncio.Event()
    stop.set()  # pre-set

    ctx = SimpleNamespace(settings=ws)
    await deep_scout_periodic_loop(ctx, stop)
    # No run because stop was set before first timeout
    assert len(called) == 0


@pytest.mark.asyncio
async def test_deep_scout_periodic_loop_one_cycle(monkeypatch: pytest.MonkeyPatch):
    """Short interval fires one cycle then stop."""
    called = []

    async def fake_run(ctx, *, periodic=False):
        called.append(1)
        return MagicMock()

    async def fake_autonomous(ctx, *, periodic=False):
        pass

    monkeypatch.setattr("init.deep_scout.run_deep_scout", fake_run)

    # Also mock the autonomous import
    import sys
    fake_dsa = MagicMock()
    fake_dsa.run_deep_scout_autonomous = AsyncMock()

    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.2)
        stop.set()

    ws = MagicMock()
    ws.deep_scout_interval_sec = 0.05  # very short

    ctx = SimpleNamespace(settings=ws)

    # Patch the import inside the function
    with patch.dict(sys.modules, {"init.deep_scout_autonomous": fake_dsa}):
        await asyncio.gather(deep_scout_periodic_loop(ctx, stop), stopper())

    # Should have run at least once
    assert len(called) >= 1
