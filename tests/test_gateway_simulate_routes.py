"""Tests for /simulate/{lane} — real per-lane synthetic alert injection."""
from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    async def send_and_wait(self, topic: str, value: bytes) -> None:
        self.sent.append((topic, value))


def _make_app(redis: FakeRedis, kafka: _KafkaCapture) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    app.state.kafka = kafka
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.state.kafka_topic_alerts = "omni-alerts"
    from gateway.routes.simulate import router
    app.include_router(router)
    return app


@pytest.fixture
def app_ctx():
    redis = FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    return _make_app(redis, kafka), redis, kafka


@pytest.mark.asyncio
async def test_list_lanes_returns_four(app_ctx):
    app, _redis, _kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/simulate/lanes")
    assert resp.status_code == 200
    lanes = resp.json()["lanes"]
    keys = {l["key"] for l in lanes}
    assert keys == {"sys_resource", "sys_hard_fail", "app_http", "siem_security"}


@pytest.mark.asyncio
async def test_unknown_lane_400(app_ctx):
    app, _redis, _kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/bogus")
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "lane,topic,label",
    [
        ("sys_resource", "omni-alerts", "SYS_RESOURCE"),
        ("sys_hard_fail", "omni-alerts", "SYS_HARD_FAIL"),
        ("app_http", "omni-diagnostic-evidence", "APP_HTTP"),
        ("siem_security", "omni-diagnostic-evidence", "SIEM_SECURITY"),
    ],
)
@pytest.mark.asyncio
async def test_lane_injects_to_correct_topic(app_ctx, lane, topic, label):
    app, redis, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/simulate/{lane}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "injected"
    assert body["lane_label"] == label
    trace_id = body["trace_id"]
    assert trace_id.startswith(f"sim-{lane}-")

    # alert lanes produce 1 message; evidence lanes produce 2 (so the batch flushes)
    expected_count = 1 if topic == "omni-alerts" else 2
    assert len(kafka.sent) == expected_count
    sent_topic, value = kafka.sent[0]
    assert sent_topic == topic
    # envelope is always {"data": "<json string>"}
    outer = json.loads(value.decode())
    inner = json.loads(outer["data"])
    if topic == "omni-alerts":
        assert inner["trace_id"] == trace_id
        # source must be "prometheus" so the normalizer runs full label extraction
        # (namespace/pod/deployment); "simulator" would fall through to GenericAlert.
        assert inner["source"] == "prometheus"
        assert inner["data"]["alerts"][0]["status"] == "firing"
    else:
        assert inner["trace_id"] == trace_id
        assert inner["lane"] == label
        # both probes carry the same trace_id but distinct probe ids
        probes = {json.loads(json.loads(v.decode())["data"])["probe"] for _t, v in kafka.sent}
        assert len(probes) == 2


@pytest.mark.asyncio
async def test_ingest_stage_marked(app_ctx):
    app, redis, _kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/sys_resource")
    trace_id = resp.json()["trace_id"]
    raw = await redis.hget(f"omni:trace:stages:{trace_id}", "INGEST")
    assert raw is not None
    entry = json.loads(raw)
    assert entry["status"] == "ok"
    # event stream got the INGEST event for SSE consumers
    events = await redis.xrange("omni:trace:events")
    assert any(f.get("trace_id") == trace_id and f.get("stage") == "INGEST" for _id, f in events)


@pytest.mark.asyncio
async def test_remote_target_routes_as_remote_agent(app_ctx):
    app, redis, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/simulate/sys_hard_fail",
            json={"target": "remote", "tenant_id": "acme", "agent_id": "host-01"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "remote"
    assert body["tenant_id"] == "acme"
    assert body["agent_id"] == "host-01"
    assert body["ingress"] == "remote_agent"
    # one probe to the evidence topic, evidence_source=RemoteAgent, FAILED, tenant carried
    assert len(kafka.sent) == 1
    topic, value = kafka.sent[0]
    assert topic == "omni-diagnostic-evidence"
    env = json.loads(json.loads(value.decode())["data"])
    assert env["evidence_source"] == "RemoteAgent"
    assert env["result"] == "FAILED"
    assert env["extracted_fact"]["result"] == "FAILED"  # drives critical urgency
    assert env["tenant_id"] == "acme"
    assert env["extracted_fact"]["agent_id"] == "host-01"


@pytest.mark.asyncio
async def test_remote_target_synthetic_agent_autogen(app_ctx):
    app, _redis, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/app_http", json={"target": "remote"})
    body = resp.json()
    assert body["agent_id"].startswith("sim-agent-")
    assert body["ingress"] == "remote_agent"


@pytest.mark.asyncio
async def test_kafka_failure_returns_502(app_ctx):
    app, redis, kafka = app_ctx

    async def _boom(topic, value):
        raise RuntimeError("broker down")

    kafka.send_and_wait = _boom  # type: ignore[assignment]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/app_http")
    assert resp.status_code == 502
