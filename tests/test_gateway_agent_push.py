"""Tests for POST /agent/v1/push — remote agent evidence ingestion."""
from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


# ── Kafka capture fake ────────────────────────────────────────────────────────

class _KafkaCapture:
    def __init__(self) -> None:
        self._sent: list[tuple[str, bytes]] = []

    async def send_and_wait(self, topic: str, value: bytes) -> None:
        self._sent.append((topic, value))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


# ── App factory ───────────────────────────────────────────────────────────────

def _make_app(redis: FakeRedis | None, kafka: _KafkaCapture | None = None) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    app.state.kafka = kafka or _KafkaCapture()
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.state.kafka_topic_alerts = "omni-alerts"
    from gateway.routes.agent_push import router
    app.include_router(router)
    return app


# ── Valid envelope fixture ────────────────────────────────────────────────────

VALID_ENVELOPE = {
    "schema_version": "1.0",
    "tenant_id": "acme-prod",
    "agent_id": "8c2a1234-abcd-4def-89ab-123456789012",
    "agent_version": "0.1.0",
    "source_type": "linux_host",
    "target_id": "host:web-01.acme.internal",
    "timestamp": "2026-05-20T10:11:12.345Z",
    "trace_id": "evt-7d2b1234abcd",
    "sequence_no": 91827,
    "evidence_type": "metrics",
    "stream_tags": ["SYS_RESOURCE"],
    "payload": {"cpu_user_pct": 87.3},
}

VALID_HEADERS = {
    "X-Omni-API-Key": "test-key-123",
    "X-Omni-Tenant-ID": "acme-prod",
    "X-Omni-Agent-ID": "8c2a1234-abcd-4def-89ab-123456789012",
}


async def _seed_auth(redis: FakeRedis, api_key: str = "test-key-123", tenant_id: str = "acme-prod") -> None:
    await redis.hset(
        f"omni:agent:tenant:{api_key}",
        mapping={"tenant_id": tenant_id, "allowed_agents": "*"},
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAgentPushRouting:
    @pytest.mark.asyncio
    async def test_valid_envelope_metrics_routes_to_evidence(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        kafka = _KafkaCapture()
        app = _make_app(redis, kafka)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=VALID_ENVELOPE, headers=VALID_HEADERS)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["trace_id"] == VALID_ENVELOPE["trace_id"]
        assert body["topic"] == "omni-diagnostic-evidence"
        assert len(kafka._sent) == 1
        topic, _ = kafka._sent[0]
        assert topic == "omni-diagnostic-evidence"

    @pytest.mark.asyncio
    async def test_valid_envelope_alert_routes_to_alerts(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        kafka = _KafkaCapture()
        app = _make_app(redis, kafka)

        envelope = {**VALID_ENVELOPE, "evidence_type": "alert", "stream_tags": ["SIEM_SECURITY"]}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=envelope, headers=VALID_HEADERS)

        assert resp.status_code == 202
        assert resp.json()["topic"] == "omni-alerts"
        topic, _ = kafka._sent[0]
        assert topic == "omni-alerts"

    @pytest.mark.asyncio
    async def test_log_event_hardfail_routes_to_evidence(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        kafka = _KafkaCapture()
        app = _make_app(redis, kafka)

        envelope = {**VALID_ENVELOPE, "evidence_type": "log_event", "stream_tags": ["SYS_HARD_FAIL"]}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=envelope, headers=VALID_HEADERS)

        assert resp.status_code == 202
        assert resp.json()["topic"] == "omni-diagnostic-evidence"
        topic, _ = kafka._sent[0]
        assert topic == "omni-diagnostic-evidence"

    @pytest.mark.asyncio
    async def test_custom_check_routes_to_evidence(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        kafka = _KafkaCapture()
        app = _make_app(redis, kafka)

        envelope = {**VALID_ENVELOPE, "evidence_type": "custom_check", "stream_tags": ["custom"]}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=envelope, headers=VALID_HEADERS)

        assert resp.status_code == 202
        assert resp.json()["topic"] == "omni-diagnostic-evidence"


class TestAgentPushAuth:
    @pytest.mark.asyncio
    async def test_missing_api_key_header_returns_401(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        app = _make_app(redis)

        headers = {k: v for k, v in VALID_HEADERS.items() if k != "X-Omni-API-Key"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=VALID_ENVELOPE, headers=headers)

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_api_key_returns_401(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        app = _make_app(redis)

        headers = {**VALID_HEADERS, "X-Omni-API-Key": "totally-wrong-key"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=VALID_ENVELOPE, headers=headers)

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_tenant_id_mismatch_returns_403(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis, tenant_id="acme-prod")
        app = _make_app(redis)

        headers = {**VALID_HEADERS, "X-Omni-Tenant-ID": "evil-corp"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=VALID_ENVELOPE, headers=headers)

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_envelope_returns_422(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        app = _make_app(redis)

        # Missing required fields: tenant_id, agent_id, etc.
        bad_envelope = {"schema_version": "1.0", "payload": {}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=bad_envelope, headers=VALID_HEADERS)

        assert resp.status_code == 422


class TestAgentPushHeartbeat:
    @pytest.mark.asyncio
    async def test_agent_heartbeat_written_to_redis(self):
        redis = FakeRedis(decode_responses=True)
        await _seed_auth(redis)
        kafka = _KafkaCapture()
        app = _make_app(redis, kafka)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/agent/v1/push", json=VALID_ENVELOPE, headers=VALID_HEADERS)

        assert resp.status_code == 202

        # Verify registry key was written
        agent_id = VALID_ENVELOPE["agent_id"]
        registry_key = f"omni:remote_agent:registry:{agent_id}"
        raw = await redis.get(registry_key)
        assert raw is not None, "Registry heartbeat not written to Redis"

        record = json.loads(raw)
        assert record["agent_id"] == agent_id
        assert record["tenant_id"] == "acme-prod"
        assert record["source_type"] == "linux_host"

        # Verify EPS ZSET has an entry
        eps_key = f"omni:remote_agent:eps:{agent_id}"
        count = await redis.zcard(eps_key)
        assert count >= 1, "EPS ZSET entry not written"
