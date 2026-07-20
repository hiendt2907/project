"""End-to-end tests for the remote-agent <-> gateway pipeline.

Philosophy (per explicit user direction — NOT a coverage-driven unit suite):
  - The REAL `remote_agent.emitter.OmniEmitter` talks to the REAL gateway
    routers (`agent_webhook.router` + `agent_commands.router`), in-process,
    via `httpx.ASGITransport`. No internal mocking of business logic on
    either side of the wire.
  - Collectors run REAL OS subprocess / psutil calls on the test machine
    (df, ls, ps, uname, systemctl-status-style commands) — these genuinely
    exist on the CI/dev host, so faking them would just be hiding the code
    under test.
  - The only fakes are for infra genuinely absent from the test environment:
    Redis -> `fakeredis.aioredis.FakeRedis` (so real ZSET/INCR rate-limit and
    dedup logic still executes for real), Kafka -> `AsyncMock` (no broker in
    CI). Real external daemons not installed in CI (mysql/proxysql/haproxy,
    a live K8s cluster) are exercised through their own real
    graceful-degradation path (collector returns None) rather than mocked —
    that IS the production behavior on a host without that service.

Where a route needs a genuinely external resource that is also unsafe/
unavailable in CI (HTTPS download with a CA-signed cert for the self-update
flow), only the network transport for that one call is faked — the guard
rails (host whitelist, scheme check, checksum) are still exercised for real.
"""
from __future__ import annotations

import json
import platform
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

from remote_agent import command_executor, emitter as emitter_mod
from remote_agent.emitter import OmniEmitter


def _build_app() -> FastAPI:
    """Real gateway app: both agent-webhook route modules mounted, no auth
    dependency wired (mirrors lab/no-auth mode used by existing gateway
    tests) — get_tenant_ctx() returns None -> admin-like, ownership checks
    are no-ops, matching how a single-tenant lab agent talks to the gateway.
    """
    from gateway.routes.agent_commands import router as commands_router
    from gateway.routes.agent_webhook import router as webhook_router

    app = FastAPI()
    app.state.redis = FakeRedis(decode_responses=True)
    app.state.kafka = AsyncMock()
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.state.kafka_topic_knowledge_evidence = "omni-knowledge-evidence"
    app.include_router(webhook_router)
    app.include_router(commands_router)
    return app


def _wire_real_emitter_to_inprocess_app(monkeypatch, app: FastAPI) -> None:
    """The only seam: point OmniEmitter's HTTP transport at the in-process
    ASGI app instead of a real socket. OmniEmitter's own logic (retries,
    payload shape, header construction) is untouched.
    """
    monkeypatch.setattr(emitter_mod, "_make_transport", lambda: httpx.ASGITransport(app=app))


def _make_emitter(monkeypatch, app: FastAPI, agent_id: str = "agent-e2e-1", tenant_id: str = "default") -> OmniEmitter:
    _wire_real_emitter_to_inprocess_app(monkeypatch, app)
    return OmniEmitter(
        gateway_url="http://test",
        api_key="test-key",
        agent_id=agent_id,
        hostname="e2e-test-host",
        tenant_id=tenant_id,
    )


# ─── Register -> threshold push -> evidence -> Kafka envelope (full real cycle) ──

class TestE2ERegisterAndEvidenceCycle:
    @pytest.mark.asyncio
    async def test_register_real_round_trip_returns_default_thresholds(self, monkeypatch):
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)

        thresholds = await agent.register(capabilities=["metrics", "logs"], version="9.9.9")

        assert thresholds == {"cpu_warn": 80.0, "mem_warn": 85.0, "disk_warn": 90.0}
        raw = await app.state.redis.get("omni:remote_agent:registry:agent-e2e-1")
        record = json.loads(raw)
        assert record["hostname"] == "e2e-test-host"
        assert record["version"] == "9.9.9"
        assert record["platform"] == platform.system().lower()

    @pytest.mark.asyncio
    async def test_register_then_real_system_metrics_emitted_through_real_pipeline(self, monkeypatch):
        """No mocking of psutil — collect_system_metrics() runs for real on
        the test machine, then the real evidence pipeline (GIGO/rate-limit/
        dedup/quality-classify) decides whether it reaches Kafka.
        """
        from remote_agent.collectors.system import collect_system_metrics

        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        thresholds = await agent.register(capabilities=["metrics"])

        fact = await collect_system_metrics("e2e-test-host", thresholds)
        assert fact is not None  # psutil is always available in this venv

        enqueued = await agent.emit([fact])

        assert enqueued == 1
        app.state.kafka.send_and_wait.assert_called_once()
        call_args = app.state.kafka.send_and_wait.call_args
        # Healthy metric samples are knowledge evidence; a real host can cross
        # the pushed threshold and legitimately become an ANOMALY.
        expected_topic = (
            "omni-diagnostic-evidence"
            if fact.get("signal_type") == "ANOMALY"
            else "omni-knowledge-evidence"
        )
        assert call_args[0][0] == expected_topic
        envelope = json.loads(json.loads(call_args[1]["value"])["data"])
        assert envelope["extracted_fact"]["agent_id"] == "agent-e2e-1"
        assert envelope["extracted_fact"]["hostname"] == "e2e-test-host"
        assert "cpu_percent" in envelope["extracted_fact"]
        assert envelope["lane"] == "SYS_RESOURCE"

    @pytest.mark.asyncio
    async def test_real_dedup_window_skips_kafka_after_pass_count(self, monkeypatch):
        """Exercises the REAL Redis INCR-based dedup logic (FakeRedis, not
        AsyncMock — needs real INCR/EXPIRE semantics), sending the identical
        evidence item repeatedly through the real /evidence pipeline.
        """
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        await agent.register(capabilities=["metrics"])

        evidence = {
            "trace_id": "ra-dedup-1",
            "probe": "remote_system_metrics",
            "result": "FAILED",
            "extracted_fact": {"cpu_percent": 99.0},
            "lane": "SYS_RESOURCE",
            "alert_hint": "CPU 99%>80%",
        }

        enqueued_counts = []
        for _ in range(5):
            enqueued_counts.append(await agent.emit([dict(evidence)]))

        # First 3 occurrences pass to Kafka, the 4th/5th are deduped (count>3).
        assert enqueued_counts == [1, 1, 1, 0, 0]
        assert app.state.kafka.send_and_wait.call_count == 3

    @pytest.mark.asyncio
    async def test_real_rate_limit_drops_excess_failed_items_same_minute(self, monkeypatch):
        """Real ZSET-based per-probe rate limiter: FAILED probe limit is 30/min
        (see agent_webhook._RL_PROBE_LIMITS) — send 32 *distinct* fingerprints
        (varying alert_hint defeats dedup) and confirm only 30 reach Kafka.
        """
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        await agent.register(capabilities=["metrics"])

        for i in range(32):
            await agent.emit([{
                "trace_id": f"ra-rl-{i}",
                "probe": "remote_log_errors",
                "result": "FAILED",
                "extracted_fact": {"i": i},
                "lane": "APP_HTTP",
                "alert_hint": f"distinct error #{i}",
            }])

        assert app.state.kafka.send_and_wait.call_count == 30

    @pytest.mark.asyncio
    async def test_circuit_breaker_real_redis_flag_blocks_real_emit(self, monkeypatch):
        app = _build_app()
        await app.state.redis.set("omni:circuit_breaker:active", "1")
        agent = _make_emitter(monkeypatch, app)

        # OmniEmitter swallows transport errors via retry+log: a 503 surfaces
        # through raise_for_status inside _post, gets retried 3x, then emit()
        # returns None (IT-7 contract: None = transport fail → caller spool
        # outbox; 0 = gateway nhận nhưng enqueue 0).
        enqueued = await agent.emit([{
            "trace_id": "ra-cb-1", "probe": "remote_system_metrics",
            "result": "PASSED", "extracted_fact": {"cpu_percent": 1.0}, "lane": "SYS_RESOURCE",
        }])
        assert enqueued is None
        app.state.kafka.send_and_wait.assert_not_called()


# ─── Command channel: enqueue (server) -> poll (real emitter) -> real ──────────
# ─── execute_batch (real subprocess) -> submit results (real emitter) ──────────

class TestE2ECommandChannelRoundTrip:
    @pytest.mark.asyncio
    async def test_full_round_trip_real_subprocess_execution(self, monkeypatch):
        """Simulates Omni-side enqueue, then drives the REAL agent-side
        poll -> execute -> submit cycle with a real read-only OS command
        (`uname -a` is in COMMAND_WHITELIST and exists on every POSIX host).
        """
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        await agent.register(capabilities=["metrics"])

        # Server-side: Omni analyst enqueues a real diagnostic command.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin_client:
            resp = await admin_client.post("/webhook/agent/commands/enqueue", json={
                "agent_id": "agent-e2e-1",
                "commands": [{"command": "uname", "args": ["-a"], "purpose": "e2e probe"}],
            })
        assert resp.status_code == 200
        assert resp.json()["enqueued"] == 1

        # Agent-side: real poll_commands() over ASGI.
        commands = await agent.poll_commands()
        assert len(commands) == 1
        assert commands[0]["command"] == "uname"

        # Agent-side: real execute_batch() — real subprocess, no mocking.
        results = await command_executor.execute_batch(commands)
        assert len(results) == 1
        assert results[0]["blocked"] is False
        assert results[0]["rc"] == 0
        assert results[0]["stdout"].strip() != ""

        # Agent-side: real submit_command_results() over ASGI.
        ok = await agent.submit_command_results([
            {"cmd_id": commands[0]["cmd_id"], **results[0]},
        ])
        assert ok is True

        stored_raw = await app.state.redis.get(f"omni:diag:cmdresult:{commands[0]['cmd_id']}")
        stored = json.loads(stored_raw)
        assert stored["rc"] == 0
        assert stored["blocked"] is False

    @pytest.mark.asyncio
    async def test_gateway_side_whitelist_blocks_non_whitelisted_command_before_queueing(self, monkeypatch):
        app = _build_app()
        _wire_real_emitter_to_inprocess_app(monkeypatch, app)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin_client:
            resp = await admin_client.post("/webhook/agent/commands/enqueue", json={
                "agent_id": "agent-e2e-1",
                "commands": [{"command": "rm", "args": ["-rf", "/"]}],
            })

        body = resp.json()
        assert body["enqueued"] == 0
        assert "rm" in body["blocked"]

    @pytest.mark.asyncio
    async def test_agent_side_executor_blocks_data_exfil_command_even_if_it_reached_queue(self, monkeypatch):
        """Defense in depth: even if a content-reading command somehow
        reached the agent (gateway bug / compromised gateway), the agent's
        OWN whitelist (command_executor._is_command_allowed) blocks it
        before any subprocess runs — verified with a real (not mocked)
        execute_batch call.
        """
        results = await command_executor.execute_batch([
            {"cmd_id": "x1", "command": "cat", "args": ["/etc/shadow"]},
        ])
        assert results[0]["blocked"] is True
        assert "data_exfil_blocked" in results[0]["block_reason"]

    @pytest.mark.asyncio
    async def test_poll_with_no_pending_commands_returns_empty(self, monkeypatch):
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        await agent.register(capabilities=["metrics"])

        commands = await agent.poll_commands()
        assert commands == []


# ─── Profile upload: real discovery scan -> real emitter -> real gateway store ──

class TestE2EProfileUpload:
    @pytest.mark.asyncio
    async def test_real_vm_discovery_scan_uploaded_and_stored(self, monkeypatch):
        from remote_agent.discovery import run_vm_discovery

        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        await agent.register(capabilities=["metrics", "discovery"])

        profile = await run_vm_discovery("agent-e2e-1", "e2e-test-host")
        assert profile["agent_id"] == "agent-e2e-1"
        assert profile.get("os_info") or "services" in profile

        ok = await agent.upload_profile(profile)

        assert ok is True
        stored_raw = await app.state.redis.get("omni:agent:profile:agent-e2e-1")
        stored = json.loads(stored_raw)
        assert stored["agent_id"] == "agent-e2e-1"
        assert stored["hostname"] == "e2e-test-host"


# ─── Self-update: real guard rails (no real network needed for these) ──────────

class TestE2ESelfUpdateGuardRails:
    """The download leg needs a real HTTPS+CA cert server, which is genuinely
    unavailable in CI — that ONE leg is faked (`updater._download`). Every
    guard rail around it (gateway-side host whitelist on enqueue, agent-side
    URL validation, checksum mismatch abort, backup-before-replace) runs for
    real — these are exactly the security-critical paths.
    """

    @pytest.mark.asyncio
    async def test_gateway_rejects_update_enqueue_without_allowed_hosts_configured(self, monkeypatch):
        monkeypatch.delenv("OMNI_AGENT_UPDATE_ALLOWED_HOSTS", raising=False)
        app = _build_app()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/webhook/agent/update", json={
                "agent_id": "agent-e2e-1",
                "version": "2.0.0",
                "download_url": "https://cdn.example.com/agent-bin",
                "sha256_checksum": "a" * 64,
            })

        assert resp.status_code == 422
        assert "url_blocked" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_gateway_enqueues_update_then_agent_executor_routes_to_updater(self, monkeypatch):
        monkeypatch.setenv("OMNI_AGENT_UPDATE_ALLOWED_HOSTS", "cdn.example.com")
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)
        await agent.register(capabilities=["metrics"])

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin_client:
            resp = await admin_client.post("/webhook/agent/update", json={
                "agent_id": "agent-e2e-1",
                "version": "2.0.0",
                "download_url": "https://cdn.example.com/agent-bin",
                "sha256_checksum": "b" * 64,
            })
        assert resp.status_code == 200

        commands = await agent.poll_commands()
        assert len(commands) == 1
        assert commands[0]["type"] == "UPDATE_AGENT"

        # Real execute_batch routes UPDATE_AGENT to the real updater, which
        # then fails the checksum (we never actually downloaded anything) —
        # proving the checksum guard runs for real even with a faked transfer.
        from remote_agent import updater

        async def fake_download(url, dest, api_key=""):
            dest.write_bytes(b"totally-different-content")
            return True, ""

        monkeypatch.setattr(updater, "_download", fake_download)
        results = await command_executor.execute_batch(commands, current_version="1.0.0")

        assert results[0]["update_status"] == "checksum_fail"

    @pytest.mark.asyncio
    async def test_agent_side_url_validation_rejects_unwhitelisted_host_even_if_gateway_misconfigured(self, monkeypatch):
        """Defense in depth: the agent re-validates the URL itself
        (INV_HOST_WHITELIST is double-enforced) using its own env var,
        independent of whatever the gateway allowed.
        """
        from remote_agent import updater

        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "trusted-cdn.example.com")
        result = await updater.handle_update_command(
            "cmd-1", "2.0.0", "https://attacker.example.com/payload", "c" * 64,
        )
        assert result["update_status"] == "url_blocked"


# ─── Tenant isolation across the real register -> evidence -> commands chain ───

class TestE2ETenantIsolation:
    @pytest.mark.asyncio
    async def test_two_tenants_evidence_does_not_cross_pollute_registry_or_metrics(self, monkeypatch):
        app = _build_app()
        agent_a = _make_emitter(monkeypatch, app, agent_id="agent-tenant-a", tenant_id="tenantA")
        await agent_a.register(capabilities=["metrics"])
        await agent_a.emit([{
            "trace_id": "ra-a-1", "probe": "remote_system_metrics", "result": "PASSED",
            "extracted_fact": {"cpu_percent": 5.0}, "lane": "SYS_RESOURCE",
        }])

        _wire_real_emitter_to_inprocess_app(monkeypatch, app)
        agent_b = OmniEmitter(
            gateway_url="http://test", api_key="k2", agent_id="agent-tenant-b",
            hostname="host-b", tenant_id="tenantB",
        )
        await agent_b.register(capabilities=["metrics"])
        await agent_b.emit([{
            "trace_id": "ra-b-1", "probe": "remote_system_metrics", "result": "PASSED",
            "extracted_fact": {"cpu_percent": 7.0}, "lane": "SYS_RESOURCE",
        }])

        reg_a = json.loads(await app.state.redis.get("omni:remote_agent:registry:agent-tenant-a"))
        reg_b = json.loads(await app.state.redis.get("omni:remote_agent:registry:agent-tenant-b"))
        assert reg_a["tenant_id"] == "tenantA"
        assert reg_b["tenant_id"] == "tenantB"

        calls = app.state.kafka.send_and_wait.call_args_list
        envelopes = [json.loads(json.loads(c[1]["value"])["data"]) for c in calls]
        tenants_seen = {e["tenant_id"] for e in envelopes}
        assert tenants_seen == {"tenantA", "tenantB"}
