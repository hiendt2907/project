"""Tests for remote agent — collectors, evidence builder, gateway endpoint."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── evidence.py ─────────────────────────────────────────────────────────────

class TestBuildEnvelope:
    def test_required_fields_present(self):
        from remote_agent.evidence import build_envelope

        env = build_envelope(
            probe="test_probe",
            lane="SYS_RESOURCE",
            result="PASSED",
            extracted_fact={"cpu": 10.0},
        )
        assert env["probe"] == "test_probe"
        assert env["lane"] == "SYS_RESOURCE"
        assert env["result"] == "PASSED"
        assert env["extracted_fact"]["cpu"] == 10.0
        assert "trace_id" in env
        assert env["trace_id"].startswith("ra-")

    def test_custom_trace_id(self):
        from remote_agent.evidence import build_envelope

        env = build_envelope(
            probe="p",
            lane="APP_HTTP",
            result="FAILED",
            extracted_fact={},
            trace_id="my-custom-trace",
        )
        assert env["trace_id"] == "my-custom-trace"

    def test_raw_truncated_to_4000(self):
        from remote_agent.evidence import build_envelope

        long_raw = "x" * 5000
        env = build_envelope(probe="p", lane="L", result="PASSED", extracted_fact={}, raw=long_raw)
        assert len(env["raw"]) == 4000

    def test_stream_tags_default(self):
        from remote_agent.evidence import build_envelope

        env = build_envelope(probe="p", lane="SYS_HARD_FAIL", result="PASSED", extracted_fact={})
        assert "SYS_HARD_FAIL" in env["stream_tags"]

    def test_lane_hint_mirrors_lane(self):
        # Agent is a sensor: the lane it stamps is a NON-AUTHORITATIVE hint.
        # Omni re-derives the authoritative proof lane via resolve_proof_lane().
        from remote_agent.evidence import build_envelope

        env = build_envelope(probe="p", lane="SYS_RESOURCE", result="PASSED", extracted_fact={})
        assert env["lane_hint"] == "SYS_RESOURCE"
        assert env["lane_authoritative"] is False


# ─── collectors/system.py ────────────────────────────────────────────────────

class TestCollectSystemMetrics:
    @pytest.mark.asyncio
    async def test_healthy_system_returns_passed(self):
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.return_value = 20.0
        fake_mem = MagicMock()
        fake_mem.percent = 40.0
        fake_mem.used = 512 * 1024 * 1024
        fake_mem.total = 2048 * 1024 * 1024
        fake_psutil.virtual_memory.return_value = fake_mem
        fake_disk = MagicMock()
        fake_disk.percent = 30.0
        fake_disk.used = 10 * 1024 ** 3
        fake_disk.total = 50 * 1024 ** 3
        fake_psutil.disk_usage.return_value = fake_disk
        fake_psutil.getloadavg.return_value = (0.5, 0.4, 0.3)

        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            result = await sys_mod.collect_system_metrics("test-host")

        assert result is not None
        assert result["result"] == "PASSED"
        assert result["extracted_fact"]["cpu_percent"] == 20.0

    @pytest.mark.asyncio
    async def test_high_cpu_returns_failed(self):
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.return_value = 95.0
        fake_mem = MagicMock()
        fake_mem.percent = 40.0
        fake_mem.used = 1024 ** 3
        fake_mem.total = 4 * 1024 ** 3
        fake_psutil.virtual_memory.return_value = fake_mem
        fake_disk = MagicMock()
        fake_disk.percent = 20.0
        fake_disk.used = 1024 ** 3
        fake_disk.total = 10 * 1024 ** 3
        fake_psutil.disk_usage.return_value = fake_disk
        fake_psutil.getloadavg.return_value = (8.0, 7.5, 6.0)

        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            result = await sys_mod.collect_system_metrics("test-host")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "CPU" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_thresholds_override_pushed_from_omni(self):
        # cpu=60 is below the default 80 (→ PASSED) but above a pushed warn of 50
        # (→ FAILED). Proves Omni-side thresholds tune the agent without redeploy.
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.return_value = 60.0
        fake_mem = MagicMock()
        fake_mem.percent = 40.0
        fake_mem.used = 1024 ** 3
        fake_mem.total = 4 * 1024 ** 3
        fake_psutil.virtual_memory.return_value = fake_mem
        fake_disk = MagicMock()
        fake_disk.percent = 20.0
        fake_disk.used = 1024 ** 3
        fake_disk.total = 10 * 1024 ** 3
        fake_psutil.disk_usage.return_value = fake_disk
        fake_psutil.getloadavg.return_value = (1.0, 1.0, 1.0)

        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            default_res = await sys_mod.collect_system_metrics("h")
            tuned_res = await sys_mod.collect_system_metrics(
                "h", {"cpu_warn": 50.0, "mem_warn": 85.0, "disk_warn": 90.0}
            )

        assert default_res["result"] == "PASSED"
        assert tuned_res["result"] == "FAILED"
        assert "CPU 60.0%>50.0%" in tuned_res["alert_hint"]

    @pytest.mark.asyncio
    async def test_psutil_missing_returns_none(self):
        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": None}):
            result = await sys_mod.collect_system_metrics("host")
        assert result is None

    @pytest.mark.asyncio
    async def test_high_mem_returns_failed(self):
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.return_value = 10.0
        fake_mem = MagicMock()
        fake_mem.percent = 90.0
        fake_mem.used = 3 * 1024 ** 3
        fake_mem.total = 4 * 1024 ** 3
        fake_psutil.virtual_memory.return_value = fake_mem
        fake_disk = MagicMock()
        fake_disk.percent = 20.0
        fake_disk.used = 1024 ** 3
        fake_disk.total = 10 * 1024 ** 3
        fake_psutil.disk_usage.return_value = fake_disk
        fake_psutil.getloadavg.return_value = (1.0, 1.0, 1.0)

        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            result = await sys_mod.collect_system_metrics("mem-host")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "MEM" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_high_disk_returns_failed(self):
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.return_value = 10.0
        fake_mem = MagicMock()
        fake_mem.percent = 30.0
        fake_mem.used = 1024 ** 3
        fake_mem.total = 8 * 1024 ** 3
        fake_psutil.virtual_memory.return_value = fake_mem
        fake_disk = MagicMock()
        fake_disk.percent = 95.0
        fake_disk.used = 9 * 1024 ** 3
        fake_disk.total = 10 * 1024 ** 3
        fake_psutil.disk_usage.return_value = fake_disk
        fake_psutil.getloadavg.return_value = (0.5, 0.5, 0.5)

        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            result = await sys_mod.collect_system_metrics("disk-host")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "DISK" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_psutil_exception_returns_none(self):
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.side_effect = RuntimeError("psutil broken")

        from remote_agent.collectors import system as sys_mod
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            result = await sys_mod.collect_system_metrics("err-host")

        assert result is None


# ─── POST /webhook/agent/register ────────────────────────────────────────────

class TestAgentRegisterEndpoint:
    @pytest.mark.asyncio
    async def test_register_stores_in_redis(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()
        fake_redis = AsyncMock()
        fake_redis.set = AsyncMock()
        app.state.redis = fake_redis
        app.state.kafka = None
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/register", json={
                "agent_id": "agent-001",
                "hostname": "server-prod-01",
                "version": "1.0.0",
                "capabilities": ["metrics", "logs"],
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "registered"
        assert body["agent_id"] == "agent-001"
        fake_redis.set.assert_called_once()
        call_args = fake_redis.set.call_args
        assert "omni:remote_agent:registry:agent-001" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_register_returns_ttl(self):
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = AsyncMock()
        app.state.redis.set = AsyncMock()

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/register", json={
                "agent_id": "a1",
                "hostname": "h1",
            })

        assert resp.json()["ttl"] == 120

    @pytest.mark.asyncio
    async def test_register_persists_domain_adapter_attestation(self):
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = AsyncMock()
        app.state.redis.set = AsyncMock()
        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/register", json={
                "agent_id": "a-domain", "hostname": "h1",
                "adapter_domains": ["linux", "database"],
            })

        assert resp.status_code == 200
        stored = json.loads(app.state.redis.set.call_args.args[1])
        assert stored["adapter_domains"] == ["database", "linux"]


# ─── POST /webhook/agent/evidence ────────────────────────────────────────────

class TestAgentEvidenceEndpoint:
    @pytest.mark.asyncio
    async def test_evidence_produces_to_kafka(self):
        from fakeredis.aioredis import FakeRedis
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = FakeRedis(decode_responses=True)

        fake_kafka = AsyncMock()
        fake_kafka.send_and_wait = AsyncMock()
        app.state.kafka = fake_kafka
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        evidence_item = {
            "trace_id": "ra-abc123",
            "probe": "remote_system_metrics",
            "result": "FAILED",
            "extracted_fact": {"cpu_percent": 92.0},
            "lane": "SYS_RESOURCE",
            "alert_hint": "CPU high",
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/evidence", json={
                "agent_id": "agent-001",
                "hostname": "server-01",
                "evidence": [evidence_item],
            })

        assert resp.status_code == 200
        assert resp.json()["enqueued"] == 1
        fake_kafka.send_and_wait.assert_called_once()
        call_args = fake_kafka.send_and_wait.call_args
        assert call_args[0][0] == "omni-diagnostic-evidence"

        # Verify envelope contains agent metadata
        payload = json.loads(json.loads(call_args[1]["value"])["data"])
        assert payload["evidence_source"] == "RemoteAgent"
        assert payload["extracted_fact"]["agent_id"] == "agent-001"
        assert payload["extracted_fact"]["hostname"] == "server-01"

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_evidence(self):
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        fake_redis = AsyncMock()
        fake_redis.get = AsyncMock(return_value="1")  # cb active
        app.state.redis = fake_redis
        app.state.kafka = AsyncMock()
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/evidence", json={
                "agent_id": "a1",
                "hostname": "h1",
                "evidence": [],
            })

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_evidence_threads_tenant_id_from_request_body(self):
        from fakeredis.aioredis import FakeRedis
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = FakeRedis(decode_responses=True)
        fake_kafka = AsyncMock()
        fake_kafka.send_and_wait = AsyncMock()
        app.state.kafka = fake_kafka
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/evidence", json={
                "agent_id": "agent-001",
                "hostname": "server-01",
                "tenant_id": "tenantA",
                "evidence": [{
                    "trace_id": "ra-tenant-1",
                    "probe": "remote_system_metrics",
                    "result": "PASSED",
                    "extracted_fact": {"cpu_percent": 10.0},
                    "lane": "SYS_RESOURCE",
                }],
            })

        assert resp.status_code == 200
        call_args = fake_kafka.send_and_wait.call_args
        payload = json.loads(json.loads(call_args[1]["value"])["data"])
        assert payload["tenant_id"] == "tenantA"

    @pytest.mark.asyncio
    async def test_evidence_defaults_tenant_id_when_omitted(self):
        from fakeredis.aioredis import FakeRedis
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = FakeRedis(decode_responses=True)
        fake_kafka = AsyncMock()
        fake_kafka.send_and_wait = AsyncMock()
        app.state.kafka = fake_kafka
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/evidence", json={
                "agent_id": "agent-001",
                "hostname": "server-01",
                "evidence": [{
                    "trace_id": "ra-tenant-2",
                    "probe": "remote_system_metrics",
                    "result": "PASSED",
                    "extracted_fact": {"cpu_percent": 10.0},
                    "lane": "SYS_RESOURCE",
                }],
            })

        assert resp.status_code == 200
        call_args = fake_kafka.send_and_wait.call_args
        payload = json.loads(json.loads(call_args[1]["value"])["data"])
        assert payload["tenant_id"] == "default"

    @pytest.mark.asyncio
    async def test_evidence_item_evidence_source_overrides_default(self):
        from fakeredis.aioredis import FakeRedis
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = FakeRedis(decode_responses=True)
        fake_kafka = AsyncMock()
        fake_kafka.send_and_wait = AsyncMock()
        app.state.kafka = fake_kafka
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/evidence", json={
                "agent_id": "agent-001",
                "hostname": "server-01",
                "tenant_id": "tenantB",
                "evidence": [{
                    "trace_id": "ra-disc-1",
                    "probe": "process_list",
                    "result": "PASSED",
                    "extracted_fact": {"discovery_data": {"processes": []}},
                    "lane": "SYS_RESOURCE",
                    "evidence_source": "DiscoveryEvidence",
                }],
            })

        assert resp.status_code == 200
        call_args = fake_kafka.send_and_wait.call_args
        payload = json.loads(json.loads(call_args[1]["value"])["data"])
        assert payload["evidence_source"] == "DiscoveryEvidence"
        assert payload["tenant_id"] == "tenantB"

    @pytest.mark.asyncio
    async def test_evidence_rejects_malformed_tenant_id(self):
        """tenant_id must match [a-zA-Z0-9_-]{1,64} — no colons/slashes that could
        be abused to escape the omni:tenant:{tenant_id}: Redis key prefix."""
        from fakeredis.aioredis import FakeRedis
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport

        app = FastAPI()
        app.state.redis = FakeRedis(decode_responses=True)
        fake_kafka = AsyncMock()
        fake_kafka.send_and_wait = AsyncMock()
        app.state.kafka = fake_kafka
        app.state.kafka_topic_evidence = "omni-diagnostic-evidence"

        from gateway.routes.agent_webhook import router
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/evidence", json={
                "agent_id": "agent-001",
                "hostname": "server-01",
                "tenant_id": "tenantA:../other",
                "evidence": [{
                    "trace_id": "ra-bad-tenant",
                    "probe": "remote_system_metrics",
                    "result": "PASSED",
                    "extracted_fact": {"cpu_percent": 10.0},
                    "lane": "SYS_RESOURCE",
                }],
            })

        assert resp.status_code == 422
        fake_kafka.send_and_wait.assert_not_called()


# ─── settings.py ─────────────────────────────────────────────────────────────

class TestAgentSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("OMNI_AGENT_GATEWAY_URL", "http://omni:8080")
        monkeypatch.setenv("OMNI_AGENT_API_KEY", "secret-key")

        from remote_agent.settings import AgentSettings
        cfg = AgentSettings()
        assert cfg.gateway_url == "http://omni:8080"
        assert cfg.api_key == "secret-key"
        assert cfg.collect_interval == 60
        assert cfg.k8s_enabled is True
        assert cfg.discovery_enabled is True

    def test_validate_raises_without_api_key(self, monkeypatch):
        monkeypatch.setenv("OMNI_AGENT_GATEWAY_URL", "http://omni:8080")
        monkeypatch.delenv("OMNI_AGENT_API_KEY", raising=False)

        from remote_agent.settings import AgentSettings
        cfg = AgentSettings()
        cfg.api_key = ""
        with pytest.raises(ValueError, match="API_KEY"):
            cfg.validate()

    def test_discovery_can_be_explicitly_disabled(self, monkeypatch):
        monkeypatch.setenv("OMNI_REMOTE_DISCOVERY_ENABLED", "false")
        monkeypatch.setenv("OMNI_AGENT_GATEWAY_URL", "http://omni:8080")
        monkeypatch.setenv("OMNI_AGENT_API_KEY", "secret-key")

        from remote_agent.settings import AgentSettings
        assert AgentSettings().discovery_enabled is False

    def test_log_paths_parsed(self, monkeypatch):
        monkeypatch.setenv("OMNI_AGENT_LOG_PATHS", "/var/log/a.log,/var/log/b.log")
        monkeypatch.setenv("OMNI_AGENT_GATEWAY_URL", "http://x")
        monkeypatch.setenv("OMNI_AGENT_API_KEY", "k")

        from remote_agent.settings import AgentSettings
        cfg = AgentSettings()
        assert cfg.log_paths == ["/var/log/a.log", "/var/log/b.log"]


# ─── agent.py ─────────────────────────────────────────────────────────────────


class TestHandleShutdown:
    def test_handle_shutdown_stops_loop(self):
        import asyncio
        from unittest.mock import MagicMock
        from remote_agent.agent import _handle_shutdown

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        _handle_shutdown(15, loop)
        loop.stop.assert_called_once()

    def test_handle_shutdown_sigint(self):
        import asyncio
        from unittest.mock import MagicMock
        from remote_agent.agent import _handle_shutdown

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        _handle_shutdown(2, loop)
        loop.stop.assert_called_once()


class TestMain:
    def test_main_runs_and_exits_on_keyboard_interrupt(self, monkeypatch):
        import asyncio
        from unittest.mock import patch, MagicMock
        from remote_agent import agent as agent_mod

        async def _raise_kb():
            raise KeyboardInterrupt

        def _ruc(coro):
            if hasattr(coro, "close"):
                coro.close()
            raise KeyboardInterrupt

        with patch.object(agent_mod, "run_agent", side_effect=_raise_kb):
            with patch("asyncio.new_event_loop") as mock_loop_factory:
                mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
                mock_loop.run_until_complete.side_effect = _ruc
                mock_loop_factory.return_value = mock_loop
                agent_mod.main()
                mock_loop.close.assert_called_once()

    def test_main_runs_and_exits_on_system_exit(self, monkeypatch):
        import asyncio
        from unittest.mock import patch, MagicMock
        from remote_agent import agent as agent_mod

        def _ruc(coro):
            if hasattr(coro, "close"):
                coro.close()
            raise SystemExit(0)

        with patch("asyncio.new_event_loop") as mock_loop_factory:
            mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
            mock_loop.run_until_complete.side_effect = _ruc
            mock_loop_factory.return_value = mock_loop
            agent_mod.main()
            mock_loop.close.assert_called_once()


class TestRunAgent:
    async def test_run_agent_one_cycle(self, monkeypatch):
        """run_agent() completes one evidence collection loop then cancels."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from remote_agent import agent as agent_mod

        call_count = 0

        async def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        fake_cfg = MagicMock()
        fake_cfg.gateway_url = "http://omni:8080"
        fake_cfg.api_key = "key"
        fake_cfg.agent_id = "test-agent"
        fake_cfg.hostname = "localhost"
        fake_cfg.k8s_enabled = False
        fake_cfg.log_paths = []
        fake_cfg.k8s_namespace = ""
        fake_cfg.collect_interval = 60
        fake_cfg.discovery_enabled = False
        fake_cfg.database_enabled = False
        fake_cfg.proxysql_enabled = False
        fake_cfg.services_enabled = False
        fake_cfg.storage_enabled = False

        fake_emitter = MagicMock()
        fake_emitter.register = AsyncMock()
        fake_emitter.emit = AsyncMock()
        fake_emitter.upload_profile = AsyncMock(return_value=True)
        fake_emitter.poll_commands = AsyncMock(return_value=[])
        fake_emitter.submit_command_results = AsyncMock(return_value=True)

        with patch("remote_agent.agent.asyncio.sleep", fake_sleep), \
             patch("remote_agent.settings.AgentSettings", return_value=fake_cfg), \
             patch("remote_agent.emitter.OmniEmitter", return_value=fake_emitter), \
             patch("remote_agent.discovery.run_vm_discovery", AsyncMock(return_value={"services": [], "packages": []})), \
             patch("remote_agent.discovery.derive_enabled_collectors", return_value={}), \
             patch("remote_agent.command_executor.execute_batch", AsyncMock(return_value=[])), \
             patch("remote_agent.collectors.system.collect_system_metrics", AsyncMock(return_value={"lane": "SYS_RESOURCE"})), \
             patch("remote_agent.collectors.logs.collect_log_errors", AsyncMock(return_value=[])):
            with pytest.raises(asyncio.CancelledError):
                await agent_mod.run_agent()

        fake_emitter.register.assert_called_once()
        assert call_count == 1

    async def test_run_agent_discovery_enabled_collects_lane7(self, monkeypatch):
        """When discovery_enabled=True, all 4 discovery probes are invoked and emitted."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from remote_agent import agent as agent_mod

        async def fake_sleep(_):
            raise asyncio.CancelledError

        fake_cfg = MagicMock()
        fake_cfg.gateway_url = "http://omni:8080"
        fake_cfg.api_key = "key"
        fake_cfg.agent_id = "test-agent"
        fake_cfg.hostname = "localhost"
        fake_cfg.k8s_enabled = False
        fake_cfg.log_paths = []
        fake_cfg.k8s_namespace = ""
        fake_cfg.collect_interval = 60
        fake_cfg.discovery_enabled = True
        fake_cfg.doc_search_dirs = ["/etc"]
        fake_cfg.database_enabled = False
        fake_cfg.proxysql_enabled = False
        fake_cfg.services_enabled = False
        fake_cfg.storage_enabled = False

        fake_emitter = MagicMock()
        fake_emitter.register = AsyncMock()
        fake_emitter.emit = AsyncMock()
        fake_emitter.upload_profile = AsyncMock(return_value=True)
        fake_emitter.poll_commands = AsyncMock(return_value=[])
        fake_emitter.submit_command_results = AsyncMock(return_value=True)

        disc_ev = {"evidence_source": "DiscoveryEvidence", "probe": "process_list"}

        with patch("remote_agent.agent.asyncio.sleep", fake_sleep), \
             patch("remote_agent.settings.AgentSettings", return_value=fake_cfg), \
             patch("remote_agent.emitter.OmniEmitter", return_value=fake_emitter), \
             patch("remote_agent.discovery.run_vm_discovery", AsyncMock(return_value={"services": [], "packages": []})), \
             patch("remote_agent.discovery.derive_enabled_collectors", return_value={}), \
             patch("remote_agent.command_executor.execute_batch", AsyncMock(return_value=[])), \
             patch("remote_agent.collectors.system.collect_system_metrics", AsyncMock(return_value=None)), \
             patch("remote_agent.collectors.logs.collect_log_errors", AsyncMock(return_value=[])), \
             patch("remote_agent.collectors.discovery_evidence.collect_process_list", AsyncMock(return_value=disc_ev)) as m_proc, \
             patch("remote_agent.collectors.discovery_evidence.collect_port_scan", AsyncMock(return_value=None)) as m_port, \
             patch("remote_agent.collectors.discovery_evidence.collect_service_topology", AsyncMock(return_value=None)) as m_svc, \
             patch("remote_agent.collectors.discovery_evidence.collect_doc_snapshot", AsyncMock(return_value=None)) as m_doc:
            with pytest.raises(asyncio.CancelledError):
                await agent_mod.run_agent()

        m_proc.assert_called_once()
        m_port.assert_called_once()
        m_svc.assert_called_once()
        m_doc.assert_called_once()
        fake_emitter.emit.assert_called_once_with([disc_ev])


# ─── collectors/services.py ──────────────────────────────────────────────────

class TestParseHaproxyCsv:
    def test_all_backends_up(self):
        from remote_agent.collectors.services import _parse_haproxy_csv

        csv = (
            "# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout,dreq,dresp,ereq,econ,eresp,"
            "wretr,wredis,status,weight,act,bck,chkfail,chkdown,lastchg,downtime,qlimit,pid,iid,"
            "sid,throttle,lbtot,tracked,type,rate,rate_lim,rate_max\n"
            "front,FRONTEND,,,10,20,100,500,10000,20000,0,0,0,,,,,OPEN,1,,,,,,,,,1,1,0,,,,0,5,0,10\n"
            "back,web1,0,0,3,5,,200,5000,10000,0,0,,,0,0,0,UP,1,1,0,0,0,100,0,,1,2,1,,200,,2,2,0,5\n"
        )
        result = _parse_haproxy_csv(csv, "host1")
        assert result["result"] == "PASSED"
        assert result["extracted_fact"]["down_backend_count"] == 0

    def test_backend_down_detected(self):
        from remote_agent.collectors.services import _parse_haproxy_csv

        csv = (
            "front,FRONTEND,,,5,10,100,200,1000,2000,0,0,0,,,,,OPEN,1,,,,,,,,1,1,0,,,0,2,0,5\n"
            "back,srv1,0,0,0,0,,0,0,0,0,0,,0,0,0,0,DOWN,1,0,0,1,1,100,100,,1,2,1,,0,,2,0,0,0\n"
        )
        result = _parse_haproxy_csv(csv, "host1")
        assert result["result"] == "FAILED"
        assert result["extracted_fact"]["down_backend_count"] >= 1
        assert result["lane"] == "SYS_HARD_FAIL"

    def test_empty_csv_returns_passed(self):
        from remote_agent.collectors.services import _parse_haproxy_csv

        result = _parse_haproxy_csv("", "host1")
        assert result["result"] == "PASSED"


class TestParseHaproxyPromMetrics:
    def test_all_servers_up(self):
        from remote_agent.collectors.services import _parse_haproxy_prom_metrics

        prom = 'haproxy_server_up{proxy="web",server="s1"} 1\n'
        result = _parse_haproxy_prom_metrics(prom, "host1")
        assert result["result"] == "PASSED"

    def test_server_down_detected(self):
        from remote_agent.collectors.services import _parse_haproxy_prom_metrics

        prom = (
            'haproxy_server_up{proxy="web",server="s1"} 1\n'
            'haproxy_server_up{proxy="db",server="primary"} 0\n'
        )
        result = _parse_haproxy_prom_metrics(prom, "host1")
        assert result["result"] == "FAILED"
        assert result["extracted_fact"]["down_backend_count"] == 1


class TestCollectSystemdUnits:
    @pytest.mark.asyncio
    async def test_all_services_ok(self):
        from remote_agent.collectors import services as svc

        with patch.object(svc, "_run", AsyncMock(return_value=("", "", 0))):
            result = await svc.collect_systemd_units("host1")

        assert result is not None
        assert result["result"] == "PASSED"
        assert result["extracted_fact"]["failed_count"] == 0

    @pytest.mark.asyncio
    async def test_failed_unit_detected(self):
        from remote_agent.collectors import services as svc

        systemctl_out = "nginx.service loaded failed failed A web server\n"
        with patch.object(svc, "_run", AsyncMock(return_value=(systemctl_out, "", 0))):
            result = await svc.collect_systemd_units("host1")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "nginx" in result["extracted_fact"]["failed_units"]

    @pytest.mark.asyncio
    async def test_any_failed_unit_is_hard_fail_regardless_of_name(self):
        """No hardcoded service-name list: a failed unit escalates to
        SYS_HARD_FAIL purely because it's failed, not because its name
        matches a fixed infra list. Confirmed live 2026-07-21: 'payment-api'
        (a customer app, not in any hardcoded list) was silently downgraded
        to SYS_RESOURCE by the old name-matching gate."""
        from remote_agent.collectors import services as svc

        systemctl_out = "payment-api.service loaded failed failed payment-api (simulated)\n"
        with patch.object(svc, "_run", AsyncMock(return_value=(systemctl_out, "", 0))), \
             patch.object(svc.pkg_origin, "get_fragment_path",
                          AsyncMock(return_value="/etc/systemd/system/payment-api.service")), \
             patch.object(svc.pkg_origin, "classify_unit_origin",
                          AsyncMock(return_value="custom")):
            result = await svc.collect_systemd_units("host1")

        assert result is not None
        assert result["lane"] == "SYS_HARD_FAIL"
        assert "payment-api" in result["extracted_fact"]["critical_failed_units"]
        assert result["extracted_fact"]["failed_units_origin"]["payment-api"] == "custom"

    @pytest.mark.asyncio
    async def test_package_owned_failure_still_hard_fail_but_not_flagged_custom(self):
        """A distro-package unit (e.g. cron) failing is still SYS_HARD_FAIL
        (any failure is a hard failure) but is correctly attributed to a real
        package, not lumped in with the customer's own app services."""
        from remote_agent.collectors import services as svc

        systemctl_out = "cron.service loaded failed failed regular background program\n"
        with patch.object(svc, "_run", AsyncMock(return_value=(systemctl_out, "", 0))), \
             patch.object(svc.pkg_origin, "get_fragment_path",
                          AsyncMock(return_value="/usr/lib/systemd/system/cron.service")), \
             patch.object(svc.pkg_origin, "classify_unit_origin",
                          AsyncMock(return_value="package:cron")):
            result = await svc.collect_systemd_units("host1")

        assert result is not None
        assert result["lane"] == "SYS_HARD_FAIL"
        assert "cron" not in result["extracted_fact"]["critical_failed_units"]
        assert result["extracted_fact"]["failed_units_origin"]["cron"] == "package:cron"

    @pytest.mark.asyncio
    async def test_systemctl_unavailable_returns_none(self):
        from remote_agent.collectors import services as svc

        with patch.object(svc, "_run", AsyncMock(return_value=("", "permission denied", 1))):
            result = await svc.collect_systemd_units("host1")

        assert result is None

    @pytest.mark.asyncio
    async def test_unit_name_ending_in_service_chars_not_mangled(self):
        """rstrip(".service") strips a CHARACTER SET, not the literal suffix —
        'payment-api.service' loses its trailing 'i' too (all of 'i','c','e',
        's','r','v' are in the strip set), becoming 'payment-ap'. Confirmed
        live 2026-07-21: this exact corruption fed a wrong unit name into the
        diagnosis LLM, which then correctly-but-uselessly concluded the
        service was "missing" after running `systemctl status payment-ap`."""
        from remote_agent.collectors import services as svc

        systemctl_out = "payment-api.service loaded failed failed payment-api (simulated)\n"
        with patch.object(svc, "_run", AsyncMock(return_value=(systemctl_out, "", 0))):
            result = await svc.collect_systemd_units("host1")

        assert result is not None
        assert "payment-api" in result["extracted_fact"]["failed_units"]
        assert "payment-ap" not in result["extracted_fact"]["failed_units"]


class TestCollectHaproxyStats:
    @pytest.mark.asyncio
    async def test_socket_success_returns_parsed(self):
        from remote_agent.collectors import services as svc

        csv_row = "front,FRONTEND,,,5,,,100,1000,2000,0,0,0,,,,,OPEN,1,,,,,,,,1,1,0,,,0,2,0,5\n"
        with patch.object(svc, "_query_haproxy_socket", AsyncMock(return_value=(csv_row, "", 0))):
            result = await svc.collect_haproxy_stats("host1")

        assert result is not None
        assert result["probe"] == "service_haproxy"

    @pytest.mark.asyncio
    async def test_socket_fail_http_fallback(self):
        from remote_agent.collectors import services as svc

        prom_text = 'haproxy_server_up{proxy="web",server="s1"} 1\n'
        with patch.object(svc, "_query_haproxy_socket", AsyncMock(return_value=("", "conn refused", 1))), \
             patch.object(svc, "_run", AsyncMock(return_value=(prom_text, "", 0))):
            result = await svc.collect_haproxy_stats("host1")

        assert result is not None
        assert result["result"] == "PASSED"

    @pytest.mark.asyncio
    async def test_both_fail_returns_none(self):
        from remote_agent.collectors import services as svc

        with patch.object(svc, "_query_haproxy_socket", AsyncMock(return_value=("", "conn refused", 1))), \
             patch.object(svc, "_run", AsyncMock(return_value=("", "failed", 1))):
            result = await svc.collect_haproxy_stats("host1")

        assert result is None


class TestServicesRunHelper:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_tuple(self):
        import asyncio as _asyncio
        from remote_agent.collectors import services as svc

        def _wf_timeout(coro, *a, **k):
            if hasattr(coro, "close"):
                coro.close()
            raise _asyncio.TimeoutError()

        with patch("remote_agent.collectors.services.asyncio.wait_for", side_effect=_wf_timeout):
            out, err, rc = await svc._run(["sleep", "999"])

        assert rc == 1
        assert "timeout" in err.lower()

    @pytest.mark.asyncio
    async def test_query_haproxy_socket_timeout(self):
        import asyncio as _asyncio
        from remote_agent.collectors import services as svc

        with patch("remote_agent.collectors.services.asyncio.open_unix_connection", side_effect=_asyncio.TimeoutError()):
            out, err, rc = await svc._query_haproxy_socket("/tmp/fake.sock", "show stat\n")

        assert rc == 1
        assert "timeout" in err.lower()


# ─── collectors/storage.py ───────────────────────────────────────────────────

_GNU_DF_HEADER = "Source              Filesystem  Size  Used  Avail  Use%  Mounted"
_GNU_DF_NORMAL = "/dev/sda1           ext4        100G   30G   70G    30%  /"
_GNU_DF_WARN = "/dev/sdb1           xfs          50G   45G    5G    90%  /data"
_GNU_DF_CRIT = "/dev/sdc1           ext4         20G   20G    0G    97%  /var"
_GNU_DF_NFS = "nfs-server:/export  nfs4        500G  200G  300G    40%  /mnt/nfs"
_GNU_DF_SKIP = "tmpfs               tmpfs         8G    2G    6G    25%  /run"


class TestCollectDiskUsage:
    @pytest.mark.asyncio
    async def test_healthy_disk_returns_passed(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_NORMAL}\n"
        inode_out = "Source  IUse%  Mounted\n/dev/sda1  10%  /\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "", 0),
            (inode_out, "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert result["result"] == "PASSED"

    @pytest.mark.asyncio
    async def test_critical_partition_returns_failed(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_CRIT}\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "", 0),
            ("Source  IUse%  Mounted\n", "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert result["result"] == "FAILED"
        assert result["lane"] == "SYS_HARD_FAIL"

    @pytest.mark.asyncio
    async def test_warn_partition_returns_inconclusive(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_WARN}\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "", 0),
            ("Source  IUse%  Mounted\n", "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        # Gateway EvidenceItem.result enum has no "WARN" member — must map to INCONCLUSIVE.
        assert result["result"] == "INCONCLUSIVE"

    @pytest.mark.asyncio
    async def test_nfs_mount_detected(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_NFS}\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "", 0),
            ("Source  IUse%  Mounted\n", "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert "/mnt/nfs" in result["extracted_fact"]["nfs_mounts"]

    @pytest.mark.asyncio
    async def test_df_fails_returns_none(self):
        from remote_agent.collectors import storage as sto

        with patch.object(sto, "_run", AsyncMock(return_value=("", "failed", 1))):
            result = await sto.collect_disk_usage("host1")

        assert result is None

    @pytest.mark.asyncio
    async def test_posix_df_fallback(self):
        from remote_agent.collectors import storage as sto

        # GNU df fails → POSIX df succeeds
        posix_out = "Filesystem  Size  Used  Avail  Use%  Mounted\n/dev/sda1   100G   30G   70G    30%  /\n"
        inode_out = "Filesystem  IUse%  Mounted\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            ("", "invalid option", 1),  # GNU df fails
            (posix_out, "", 0),          # POSIX df succeeds
            (inode_out, "", 0),          # inode check
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert result["result"] == "PASSED"

    @pytest.mark.asyncio
    async def test_inode_critical_detected(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_NORMAL}\n"
        inode_out = "Source  IUse%  Mounted\n/dev/sda1  97%  /\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "", 0),
            (inode_out, "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert result["result"] == "FAILED"
        assert len(result["extracted_fact"]["inode_critical"]) == 1

    @pytest.mark.asyncio
    async def test_tmpfs_skipped(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_SKIP}\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "", 0),
            ("Source  IUse%  Mounted\n", "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert len(result["extracted_fact"]["partitions"]) == 0

    @pytest.mark.asyncio
    async def test_df_partial_failure_still_parses(self):
        from remote_agent.collectors import storage as sto

        df_out = f"{_GNU_DF_HEADER}\n{_GNU_DF_NORMAL}\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (df_out, "some mount inaccessible", 1),  # rc=1 but has output
            ("Source  IUse%  Mounted\n", "", 0),
        ])):
            result = await sto.collect_disk_usage("host1")

        assert result is not None
        assert result["result"] == "PASSED"


class TestCollectNfsHealth:
    @pytest.mark.asyncio
    async def test_no_nfs_mounts_returns_none(self):
        from remote_agent.collectors import storage as sto

        mounts_out = "tmpfs /run tmpfs rw 0 0\n/dev/sda1 / ext4 rw 0 0\n"
        with patch.object(sto, "_run", AsyncMock(return_value=(mounts_out, "", 0))):
            result = await sto.collect_nfs_health("host1")

        assert result is None

    @pytest.mark.asyncio
    async def test_proc_mounts_fail_returns_none(self):
        from remote_agent.collectors import storage as sto

        with patch.object(sto, "_run", AsyncMock(return_value=("", "failed", 1))):
            result = await sto.collect_nfs_health("host1")

        assert result is None

    @pytest.mark.asyncio
    async def test_healthy_nfs_returns_passed(self):
        from remote_agent.collectors import storage as sto

        mounts_out = "nfs-server:/export /mnt/nfs nfs4 rw 0 0\n"
        # stat success, dmesg no nfs errors
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (mounts_out, "", 0),     # /proc/mounts
            ("stat output", "", 0),  # stat /mnt/nfs
            ("", "", 0),             # dmesg
        ])):
            result = await sto.collect_nfs_health("host1")

        assert result is not None
        assert result["result"] == "PASSED"
        assert result["extracted_fact"]["nfs_mounts_total"] == 1

    @pytest.mark.asyncio
    async def test_stale_nfs_mount_detected(self):
        from remote_agent.collectors import storage as sto

        mounts_out = "nfs-server:/export /mnt/nfs nfs4 rw 0 0\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (mounts_out, "", 0),             # /proc/mounts
            ("", "stale file handle", 1),    # stat returns stale
            ("kernel: nfs: stale handle", "", 0),  # dmesg
        ])):
            result = await sto.collect_nfs_health("host1")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "/mnt/nfs" in result["extracted_fact"]["stale_mounts"]

    @pytest.mark.asyncio
    async def test_io_error_nfs_mount_detected(self):
        from remote_agent.collectors import storage as sto

        mounts_out = "nfs-server:/export /mnt/nfs nfs4 rw 0 0\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (mounts_out, "", 0),
            ("", "input/output error", 1),
            ("", "", 0),
        ])):
            result = await sto.collect_nfs_health("host1")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "/mnt/nfs" in result["extracted_fact"]["io_error_mounts"]

    @pytest.mark.asyncio
    async def test_stat_nonzero_rc_flagged_as_io_error(self):
        from remote_agent.collectors import storage as sto

        mounts_out = "nfs-server:/vol /mnt/data nfs rw 0 0\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (mounts_out, "", 0),
            ("", "connection refused", 1),  # stat fails, non-stale error
            ("", "", 0),
        ])):
            result = await sto.collect_nfs_health("host1")

        assert result is not None
        assert "/mnt/data" in result["extracted_fact"]["io_error_mounts"]

    @pytest.mark.asyncio
    async def test_dmesg_nfs_errors_captured(self):
        from remote_agent.collectors import storage as sto

        mounts_out = "nfs-server:/export /mnt/nfs nfs4 rw 0 0\n"
        dmesg_out = "kernel: nfs: server not responding, timeout\n"
        with patch.object(sto, "_run", AsyncMock(side_effect=[
            (mounts_out, "", 0),
            ("ok", "", 0),
            (dmesg_out, "", 0),
        ])):
            result = await sto.collect_nfs_health("host1")

        assert result is not None
        assert len(result["extracted_fact"]["nfs_dmesg_errors"]) == 1


# ─── collectors/discovery_evidence.py (step-2) ───────────────────────────────

class TestCollectProcessList:
    @pytest.mark.asyncio
    async def test_counts_processes_returns_discovery_evidence(self):
        from remote_agent.collectors import discovery_evidence as disc

        ps_out = "COMMAND\nnginx\nnginx\nsshd\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(ps_out, 0))):
            result = await disc.collect_process_list("host1")

        assert result is not None
        assert result["evidence_source"] == "DiscoveryEvidence"
        procs = result["extracted_fact"]["discovery_data"]["processes"]
        assert {"name": "nginx", "count": 2} in procs
        assert {"name": "sshd", "count": 1} in procs

    @pytest.mark.asyncio
    async def test_ps_unavailable_returns_none(self):
        from remote_agent.collectors import discovery_evidence as disc

        with patch.object(disc, "_run", AsyncMock(return_value=("", 1))):
            result = await disc.collect_process_list("host1")

        assert result is None


class TestCollectPortScan:
    @pytest.mark.asyncio
    async def test_ss_success_parses_listening_ports(self):
        from remote_agent.collectors import discovery_evidence as disc

        ss_out = 'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1,fd=6))\n'
        with patch.object(disc, "_run", AsyncMock(return_value=(ss_out, 0))):
            result = await disc.collect_port_scan("host1")

        assert result is not None
        ports = result["extracted_fact"]["discovery_data"]["listening_ports"]
        assert {"port": 8080, "service": "nginx"} in ports

    @pytest.mark.asyncio
    async def test_ss_fails_falls_back_to_netstat(self):
        from remote_agent.collectors import discovery_evidence as disc

        netstat_out = 'tcp 0 0 0.0.0.0:443 0.0.0.0:* LISTEN 1/nginx\n'
        with patch.object(disc, "_run", AsyncMock(side_effect=[("", 1), (netstat_out, 0)])):
            result = await disc.collect_port_scan("host1")

        assert result is not None
        ports = result["extracted_fact"]["discovery_data"]["listening_ports"]
        assert any(p["port"] == 443 for p in ports)

    @pytest.mark.asyncio
    async def test_both_unavailable_returns_none(self):
        from remote_agent.collectors import discovery_evidence as disc

        with patch.object(disc, "_run", AsyncMock(return_value=("", 1))):
            result = await disc.collect_port_scan("host1")

        assert result is None


class TestCollectServiceTopology:
    @pytest.mark.asyncio
    async def test_running_units_returns_discovery_evidence(self):
        from remote_agent.collectors import discovery_evidence as disc

        systemctl_out = "nginx.service loaded active running A web server\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))):
            result = await disc.collect_service_topology("host1")

        assert result is not None
        services = result["extracted_fact"]["discovery_data"]["services"]
        assert services[0]["name"] == "nginx"
        assert services[0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_service_name_with_service_chars_not_mangled(self):
        """rstrip(".service") corrupts names ending in those chars (e.g. 'redis' -> 'red')."""
        from remote_agent.collectors import discovery_evidence as disc

        systemctl_out = "redis.service loaded active running In-memory store\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))):
            result = await disc.collect_service_topology("host1")

        assert result is not None
        services = result["extracted_fact"]["discovery_data"]["services"]
        assert services[0]["name"] == "redis"

    @pytest.mark.asyncio
    async def test_systemctl_unavailable_returns_none(self):
        from remote_agent.collectors import discovery_evidence as disc

        with patch.object(disc, "_run", AsyncMock(return_value=("", 1))):
            result = await disc.collect_service_topology("host1")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_state_filter_passed_to_systemctl(self):
        """Same anti-pattern already fixed in discovery.py::_collect_running_services
        (2026-07-21): a --state=running filter makes a crashed/failed unit
        invisible to the onboarding topology snapshot exactly when it crashes —
        the one moment the System Twin/entity graph most needs to know the
        service exists. A failed unit stays loaded/"in memory" until
        reset-failed, so it must still show up here, just with its real status."""
        from remote_agent.collectors import discovery_evidence as disc

        run_mock = AsyncMock(return_value=("", 0))
        with patch.object(disc, "_run", run_mock):
            await disc.collect_service_topology("host1")

        args = run_mock.await_args.args[0]
        assert "--state=running" not in args

    @pytest.mark.asyncio
    async def test_failed_unit_still_discovered_with_real_status(self):
        """A crashed unit must survive into the topology snapshot with its
        actual state, not be silently dropped nor mislabeled 'running'."""
        from remote_agent.collectors import discovery_evidence as disc

        systemctl_out = "payment-api.service loaded failed failed payment-api (simulated)\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))):
            result = await disc.collect_service_topology("host1")

        assert result is not None
        services = result["extracted_fact"]["discovery_data"]["services"]
        assert services[0]["name"] == "payment-api"
        assert services[0]["status"] == "failed"


class TestCollectConnectionScan:
    @pytest.mark.asyncio
    async def test_ss_success_parses_established_connections(self):
        from remote_agent.collectors import discovery_evidence as disc

        ss_out = (
            "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            'ESTAB  0      0      10.0.0.5:44444      10.0.0.9:6379     users:(("app",pid=123,fd=7))\n'
        )
        with patch.object(disc, "_run", AsyncMock(return_value=(ss_out, 0))):
            result = await disc.collect_connection_scan("host1")

        assert result is not None
        assert result["probe"] == "connection_scan"
        conns = result["extracted_fact"]["discovery_data"]["connections"]
        assert {
            "local_port": 44444, "remote_ip": "10.0.0.9", "remote_port": 6379, "process": "app",
        } in conns

    @pytest.mark.asyncio
    async def test_listen_only_lines_are_ignored(self):
        """connection_scan must not double-count port_scan's LISTEN rows."""
        from remote_agent.collectors import discovery_evidence as disc

        ss_out = 'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n'
        with patch.object(disc, "_run", AsyncMock(return_value=(ss_out, 0))):
            result = await disc.collect_connection_scan("host1")

        assert result is not None
        assert result["extracted_fact"]["discovery_data"]["connections"] == []

    @pytest.mark.asyncio
    async def test_ss_fails_falls_back_to_netstat(self):
        from remote_agent.collectors import discovery_evidence as disc

        netstat_out = "tcp 0 0 10.0.0.5:44444 10.0.0.9:6379 ESTABLISHED 123/app\n"
        with patch.object(disc, "_run", AsyncMock(side_effect=[("", 1), (netstat_out, 0)])):
            result = await disc.collect_connection_scan("host1")

        assert result is not None
        conns = result["extracted_fact"]["discovery_data"]["connections"]
        assert any(c["remote_ip"] == "10.0.0.9" and c["remote_port"] == 6379 for c in conns)

    @pytest.mark.asyncio
    async def test_both_unavailable_returns_none(self):
        from remote_agent.collectors import discovery_evidence as disc

        with patch.object(disc, "_run", AsyncMock(return_value=("", 1))):
            result = await disc.collect_connection_scan("host1")

        assert result is None


class TestCollectDocSnapshot:
    @pytest.mark.asyncio
    async def test_finds_readme_returns_hash_reference_not_content(self, tmp_path):
        """INV_DATA_RESIDENCY: raw text never enters the envelope — hash at source."""
        import hashlib
        import json

        from remote_agent.collectors import discovery_evidence as disc

        text = "# My Service\nDoes things."
        (tmp_path / "README.md").write_text(text)

        result = await disc.collect_doc_snapshot("host1", [str(tmp_path)])

        assert result is not None
        docs = result["extracted_fact"]["discovery_data"]["documents"]
        assert len(docs) == 1
        assert "content" not in docs[0]
        assert docs[0]["content_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert docs[0]["content_length"] == len(text)
        assert isinstance(docs[0]["mtime"], int)
        # Belt-and-braces: the raw text must not appear ANYWHERE in the envelope.
        assert "My Service" not in json.dumps(result)

    @pytest.mark.asyncio
    async def test_hash_matches_legacy_server_side_truncation_window(self, tmp_path):
        """Files larger than _DOC_MAX_BYTES hash the same truncated window the
        legacy Omni-side sanitizer hashed — hashes stay comparable across versions."""
        import hashlib

        from remote_agent.collectors import discovery_evidence as disc

        big = "x" * (disc._DOC_MAX_BYTES + 500)
        (tmp_path / "README.md").write_text(big)

        result = await disc.collect_doc_snapshot("host1", [str(tmp_path)])

        docs = result["extracted_fact"]["discovery_data"]["documents"]
        truncated = big[: disc._DOC_MAX_BYTES]
        assert docs[0]["content_hash"] == hashlib.sha256(truncated.encode("utf-8")).hexdigest()
        assert docs[0]["content_length"] == disc._DOC_MAX_BYTES

    @pytest.mark.asyncio
    async def test_no_docs_found_returns_none(self, tmp_path):
        from remote_agent.collectors import discovery_evidence as disc

        result = await disc.collect_doc_snapshot("host1", [str(tmp_path)])

        assert result is None

    @pytest.mark.asyncio
    async def test_symlink_escape_not_followed(self, tmp_path):
        """A symlinked README must not be read — prevents exfiltrating arbitrary files."""
        from remote_agent.collectors import discovery_evidence as disc

        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        secret_file = secret_dir / "id_rsa"
        secret_file.write_text("-----BEGIN PRIVATE KEY-----")

        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        (scan_dir / "README.md").symlink_to(secret_file)

        result = await disc.collect_doc_snapshot("host1", [str(scan_dir)])

        assert result is None

    @pytest.mark.asyncio
    async def test_nonexistent_dir_skipped_safely(self):
        from remote_agent.collectors import discovery_evidence as disc

        result = await disc.collect_doc_snapshot("host1", ["/nonexistent/dir/xyz"])

        assert result is None


# ─── remote_host_baseline.py (1C) ────────────────────────────────────────────

class TestRemoteHostBaseline:
    @pytest.mark.asyncio
    async def test_returns_empty_when_redis_none(self):
        from anomaly.remote_host_baseline import update_remote_host_baseline

        out = await update_remote_host_baseline(
            None, tenant_id="t1", host="h1", fact={"cpu_percent": 10.0}
        )
        assert out == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_host(self):
        from fakeredis.aioredis import FakeRedis
        from anomaly.remote_host_baseline import update_remote_host_baseline

        r = FakeRedis(decode_responses=True)
        out = await update_remote_host_baseline(
            r, tenant_id="t1", host="", fact={"cpu_percent": 10.0}
        )
        assert out == {}

    @pytest.mark.asyncio
    async def test_insufficient_samples_no_zscore(self):
        from fakeredis.aioredis import FakeRedis
        from anomaly.remote_host_baseline import update_remote_host_baseline

        r = FakeRedis(decode_responses=True)
        out = await update_remote_host_baseline(
            r, tenant_id="t1", host="h1", fact={"cpu_percent": 10.0}
        )
        assert "z_cpu" not in out  # < 3 samples → no z yet

    @pytest.mark.asyncio
    async def test_computes_zscore_after_window(self):
        from fakeredis.aioredis import FakeRedis
        from anomaly.remote_host_baseline import update_remote_host_baseline

        r = FakeRedis(decode_responses=True)
        # feed a stable baseline, then a spike
        for v in (10.0, 11.0, 9.0, 10.0):
            await update_remote_host_baseline(
                r, tenant_id="t1", host="h1", fact={"cpu_percent": v}
            )
        out = await update_remote_host_baseline(
            r, tenant_id="t1", host="h1", fact={"cpu_percent": 95.0}
        )
        assert "z_cpu" in out
        assert out["z_cpu"] > 1.0  # spike is far above the host baseline

    @pytest.mark.asyncio
    async def test_per_host_isolation(self):
        from fakeredis.aioredis import FakeRedis
        from anomaly.remote_host_baseline import update_remote_host_baseline

        r = FakeRedis(decode_responses=True)
        for v in (10.0, 11.0, 9.0):
            await update_remote_host_baseline(
                r, tenant_id="t1", host="h1", fact={"mem_percent": v}
            )
        # different host has its own (empty) window
        out = await update_remote_host_baseline(
            r, tenant_id="t1", host="h2", fact={"mem_percent": 50.0}
        )
        assert "z_mem" not in out  # h2 only has 1 sample

    @pytest.mark.asyncio
    async def test_non_numeric_fact_skipped(self):
        from fakeredis.aioredis import FakeRedis
        from anomaly.remote_host_baseline import update_remote_host_baseline

        r = FakeRedis(decode_responses=True)
        out = await update_remote_host_baseline(
            r, tenant_id="t1", host="h1", fact={"cpu_percent": "n/a"}
        )
        assert out == {}


class TestDiscoveryCollectRunningServices:
    """remote_agent/discovery.py::_collect_running_services shares the same
    rstrip(".service") character-set bug found live 2026-07-21 in
    collectors/services.py — 'payment-api.service' -> 'payment-ap' since
    rstrip strips a char SET, not the literal suffix."""

    @pytest.mark.asyncio
    async def test_unit_name_ending_in_service_chars_not_mangled(self):
        from remote_agent import discovery as disc

        systemctl_out = "payment-api.service loaded active running payment-api (simulated)\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))):
            services = await disc._collect_running_services()

        assert len(services) == 1
        assert services[0]["name"] == "payment-api"

    @pytest.mark.asyncio
    async def test_unmangled_name_still_works(self):
        from remote_agent import discovery as disc

        systemctl_out = "nginx.service loaded active running A web server\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))):
            services = await disc._collect_running_services()

        assert services[0]["name"] == "nginx"

    @pytest.mark.asyncio
    async def test_no_state_filter_passed_to_systemctl(self):
        """Found live 2026-07-21: a --state=running filter made a crashed unit
        invisible to discovery exactly when it crashed, which fed into
        collectors/services.py's critical-service gate and silently downgraded
        the unit's own failure evidence from lane=SYS_HARD_FAIL to
        lane=SYS_RESOURCE — reproduced live for payment-api.service on a real
        VM. A failed unit stays loaded/"in memory" until reset-failed, so
        dropping the state filter (not just widening it) is what keeps it
        discoverable."""
        from remote_agent import discovery as disc

        run_mock = AsyncMock(return_value=("", 0))
        with patch.object(disc, "_run", run_mock):
            await disc._collect_running_services()

        args = run_mock.await_args.args[0]
        assert "--state=running" not in args

    @pytest.mark.asyncio
    async def test_failed_unit_still_discovered_with_real_status(self):
        from remote_agent import discovery as disc

        systemctl_out = "payment-api.service loaded failed failed payment-api (simulated)\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))):
            services = await disc._collect_running_services()

        assert services[0]["name"] == "payment-api"
        assert services[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_origin_tagged_via_package_manager_not_hardcoded(self):
        from remote_agent import discovery as disc

        systemctl_out = "payment-api.service loaded active running payment-api (simulated)\n"
        with patch.object(disc, "_run", AsyncMock(return_value=(systemctl_out, 0))), \
             patch.object(disc.pkg_origin, "get_fragment_path",
                          AsyncMock(return_value="/etc/systemd/system/payment-api.service")), \
             patch.object(disc.pkg_origin, "classify_unit_origin",
                          AsyncMock(return_value="custom")):
            services = await disc._collect_running_services()

        assert services[0]["origin"] == "custom"


class TestPkgOrigin:
    """pkg_origin.py — classify a systemd unit's origin (base OS package vs
    the customer's own app) by asking the real package manager who owns its
    FragmentPath. No hardcoded service-name list anywhere in this module."""

    @pytest.mark.asyncio
    async def test_dpkg_owned_file_returns_package_name(self):
        from remote_agent import pkg_origin

        with patch.object(pkg_origin, "_DPKG", "/usr/bin/dpkg"), \
             patch.object(pkg_origin, "_RPM", None), \
             patch.object(pkg_origin, "_run",
                          AsyncMock(return_value=("nginx-common: /usr/lib/systemd/system/nginx.service\n", "", 0))):
            origin = await pkg_origin.classify_unit_origin("/usr/lib/systemd/system/nginx.service")

        assert origin == "package:nginx-common"

    @pytest.mark.asyncio
    async def test_dpkg_unowned_file_is_custom(self):
        from remote_agent import pkg_origin

        with patch.object(pkg_origin, "_DPKG", "/usr/bin/dpkg"), \
             patch.object(pkg_origin, "_RPM", None), \
             patch.object(pkg_origin, "_run",
                          AsyncMock(return_value=("", "dpkg-query: no path found matching pattern", 1))):
            origin = await pkg_origin.classify_unit_origin("/etc/systemd/system/payment-api.service")

        assert origin == "custom"

    @pytest.mark.asyncio
    async def test_rpm_owned_file_returns_package_name(self):
        from remote_agent import pkg_origin

        with patch.object(pkg_origin, "_DPKG", None), \
             patch.object(pkg_origin, "_RPM", "/usr/bin/rpm"), \
             patch.object(pkg_origin, "_run", AsyncMock(return_value=("mariadb-server\n", "", 0))):
            origin = await pkg_origin.classify_unit_origin("/usr/lib/systemd/system/mariadb.service")

        assert origin == "package:mariadb-server"

    @pytest.mark.asyncio
    async def test_no_package_manager_available_is_unknown(self):
        from remote_agent import pkg_origin

        with patch.object(pkg_origin, "_DPKG", None), patch.object(pkg_origin, "_RPM", None):
            origin = await pkg_origin.classify_unit_origin("/etc/systemd/system/payment-api.service")

        assert origin == "unknown"

    @pytest.mark.asyncio
    async def test_empty_fragment_path_is_unknown(self):
        from remote_agent import pkg_origin

        origin = await pkg_origin.classify_unit_origin("")

        assert origin == "unknown"

    @pytest.mark.asyncio
    async def test_get_fragment_path_returns_value_on_success(self):
        from remote_agent import pkg_origin

        with patch.object(pkg_origin, "_run",
                          AsyncMock(return_value=("/etc/systemd/system/payment-api.service\n", "", 0))):
            path = await pkg_origin.get_fragment_path("payment-api.service")

        assert path == "/etc/systemd/system/payment-api.service"

    @pytest.mark.asyncio
    async def test_get_fragment_path_empty_on_failure(self):
        from remote_agent import pkg_origin

        with patch.object(pkg_origin, "_run", AsyncMock(return_value=("", "no such unit", 1))):
            path = await pkg_origin.get_fragment_path("nonexistent.service")

        assert path == ""
