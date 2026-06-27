"""Coverage tests for remote_agent/emitter.py, collectors/logs.py, collectors/k8s.py."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── emitter.py ────────────────────────────────────────────────────────────────

class TestOmniEmitter:
    def _emitter(self):
        from remote_agent.emitter import OmniEmitter
        return OmniEmitter(
            gateway_url="http://gateway:8080",
            api_key="test-key",
            agent_id="agent-1",
            hostname="host-1",
        )

    def test_init(self):
        e = self._emitter()
        assert e._agent_id == "agent-1"
        assert e._hostname == "host-1"
        assert "Bearer test-key" in e._headers["Authorization"]

    @pytest.mark.asyncio
    async def test_register_success(self):
        e = self._emitter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ttl": 120,
            "config": {"thresholds": {"cpu_warn": 70.0, "mem_warn": 75.0, "disk_warn": 88.0}},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("remote_agent.emitter._make_client", return_value=mock_client):
            result = await e.register(["logs", "k8s"], version="1.0.1")
        # register returns the threshold bundle pushed by Omni (or None on failure)
        assert result == {"cpu_warn": 70.0, "mem_warn": 75.0, "disk_warn": 88.0}

    @pytest.mark.asyncio
    async def test_register_fail_returns_none(self):
        e = self._emitter()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=RuntimeError("conn refused"))
        with patch("remote_agent.emitter._make_client", return_value=mock_client):
            result = await e.register(["logs"])
        assert result is None

    @pytest.mark.asyncio
    async def test_register_no_config_returns_none(self):
        # Older gateway without threshold support → no config block → None.
        e = self._emitter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ttl": 120}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("remote_agent.emitter._make_client", return_value=mock_client):
            result = await e.register(["logs"])
        assert result is None

    @pytest.mark.asyncio
    async def test_emit_empty_returns_zero(self):
        e = self._emitter()
        result = await e.emit([])
        assert result == 0

    @pytest.mark.asyncio
    async def test_emit_success(self):
        e = self._emitter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"enqueued": 3}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("remote_agent.emitter._make_client", return_value=mock_client):
            result = await e.emit([{"probe": "test"}] * 3)
        assert result == 3

    @pytest.mark.asyncio
    async def test_emit_fail_returns_zero(self):
        e = self._emitter()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=RuntimeError("network"))
        with patch("remote_agent.emitter._make_client", return_value=mock_client):
            result = await e.emit([{"probe": "test"}])
        assert result == 0


def test_make_transport():
    from remote_agent.emitter import _make_transport
    transport = _make_transport()
    assert transport is not None


def test_make_client():
    from remote_agent.emitter import _make_client
    client = _make_client({"Authorization": "Bearer k"}, "http://gw:8080")
    assert client is not None


# ── collectors/logs.py ────────────────────────────────────────────────────────

class TestLogCollector:
    def test_tail_lines_empty_file(self):
        from remote_agent.collectors.logs import _tail_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            fname = f.name
        try:
            lines = _tail_lines(fname, 10)
            assert isinstance(lines, list)
        finally:
            os.unlink(fname)

    def test_tail_lines_nonexistent(self):
        from remote_agent.collectors.logs import _tail_lines
        lines = _tail_lines("/tmp/does_not_exist_xyz.log", 10)
        assert lines == []

    def test_tail_lines_with_content(self):
        from remote_agent.collectors.logs import _tail_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            for i in range(20):
                f.write(f"2024-01-01 INFO line {i}\n")
            fname = f.name
        try:
            lines = _tail_lines(fname, 5)
            assert len(lines) == 5
        finally:
            os.unlink(fname)

    @pytest.mark.asyncio
    async def test_collect_no_paths(self):
        from remote_agent.collectors.logs import collect_log_errors
        results = await collect_log_errors([], "host-1")
        assert results == []

    @pytest.mark.asyncio
    async def test_collect_no_match_glob(self):
        from remote_agent.collectors.logs import collect_log_errors
        results = await collect_log_errors(["/tmp/no_such_logs_xyz*.log"], "host-1")
        assert results == []

    @pytest.mark.asyncio
    async def test_collect_below_threshold(self):
        from remote_agent.collectors.logs import collect_log_errors
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("INFO everything is fine\n" * 10)
            fname = f.name
        try:
            results = await collect_log_errors([fname], "host-1")
            assert len(results) == 1
            assert results[0]["result"] == "PASSED"
        finally:
            os.unlink(fname)

    @pytest.mark.asyncio
    async def test_collect_above_threshold(self):
        from remote_agent.collectors.logs import collect_log_errors, _ERROR_THRESHOLD
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            for _ in range(_ERROR_THRESHOLD + 2):
                f.write("ERROR: something failed badly\n")
            fname = f.name
        try:
            results = await collect_log_errors([fname], "host-1")
            assert len(results) == 1
            assert results[0]["result"] == "FAILED"
            assert results[0]["extracted_fact"]["failed_file_count"] == 1
            assert results[0]["extracted_fact"]["failed_files"][0]["error_count"] >= _ERROR_THRESHOLD
        finally:
            os.unlink(fname)


# ── collectors/k8s.py ─────────────────────────────────────────────────────────

class TestK8sCollector:
    @pytest.mark.asyncio
    async def test_returns_empty_if_kubernetes_not_installed(self):
        from remote_agent.collectors.k8s import collect_k8s_status
        with patch.dict("sys.modules", {
            "kubernetes_asyncio": None,
            "kubernetes_asyncio.client": None,
            "kubernetes_asyncio.config": None,
            "kubernetes_asyncio.client.rest": None,
        }):
            result = await collect_k8s_status("multi-agent", "host-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_if_no_kubeconfig(self):
        from remote_agent.collectors.k8s import collect_k8s_status
        mock_config = MagicMock()
        mock_config.load_kube_config = AsyncMock(side_effect=Exception("no kubeconfig"))
        mock_config.load_incluster_config = MagicMock(side_effect=Exception("not in cluster"))
        mock_k8s = MagicMock()
        mock_k8s.client = MagicMock()
        mock_k8s.config = mock_config
        mock_k8s.client.rest.ApiException = Exception
        with patch.dict("sys.modules", {
            "kubernetes_asyncio": mock_k8s,
            "kubernetes_asyncio.client": mock_k8s.client,
            "kubernetes_asyncio.config": mock_config,
            "kubernetes_asyncio.client.rest": mock_k8s.client.rest,
        }):
            result = await collect_k8s_status("multi-agent", "host-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_all_pods_healthy(self):
        from remote_agent.collectors.k8s import collect_k8s_status

        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.namespace = "default"
        cond = MagicMock(); cond.type = "Ready"; cond.ready = True
        mock_pod.status.conditions = [cond]

        pod_list = MagicMock(); pod_list.items = [mock_pod]

        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod = AsyncMock(return_value=pod_list)

        mock_api_client = AsyncMock()
        mock_api_client.__aenter__ = AsyncMock(return_value=mock_api_client)
        mock_api_client.__aexit__ = AsyncMock(return_value=None)

        mock_config = MagicMock()
        mock_config.load_kube_config = AsyncMock()
        mock_config.load_incluster_config = MagicMock()

        MockApiException = type("ApiException", (Exception,), {})
        mock_client_mod = MagicMock()
        mock_client_mod.ApiClient = MagicMock(return_value=mock_api_client)
        mock_client_mod.CoreV1Api = MagicMock(return_value=mock_v1)
        mock_client_mod.rest.ApiException = MockApiException

        mock_k8s = MagicMock()
        mock_k8s.client = mock_client_mod
        mock_k8s.config = mock_config

        with patch.dict("sys.modules", {
            "kubernetes_asyncio": mock_k8s,
            "kubernetes_asyncio.client": mock_client_mod,
            "kubernetes_asyncio.config": mock_config,
            "kubernetes_asyncio.client.rest": mock_client_mod.rest,
        }):
            result = await collect_k8s_status("default", "host-1")

        assert len(result) == 1
        assert result[0]["result"] == "PASSED"
