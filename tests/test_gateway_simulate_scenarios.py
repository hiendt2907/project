"""Tests cho /simulate/scenario/{scenario} — nút "Test lại" trang /diagnostics.

Bug gốc được khoá lại ở đây: portal từng gọi `/simulate/state` và `/simulate/resource`
(đó là `proof_lane`, trục B) trong khi simulator chỉ nhận lane key trục A
(sys_resource/sys_hard_fail/app_http/siem_security) ⇒ cả 4 nút trả 400.
"""
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


@pytest.fixture
def app_ctx():
    redis = FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    app = FastAPI()
    app.state.redis = redis
    app.state.kafka = kafka
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.state.kafka_topic_alerts = "omni-alerts"
    from gateway.routes.simulate import router

    app.include_router(router)
    return app, redis, kafka


def _envelope(kafka: _KafkaCapture) -> dict:
    assert len(kafka.sent) == 1
    return json.loads(json.loads(kafka.sent[0][1].decode("utf-8"))["data"])


@pytest.mark.asyncio
async def test_scenario_catalog_matches_portal_buttons(app_ctx):
    app, _r, _k = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/simulate/scenarios")
    assert resp.status_code == 200
    keys = {s["key"] for s in resp.json()["scenarios"]}
    # Khớp đúng 4 nút trong ui/apps/provider-portal/app/diagnostics/TestPanel.tsx.
    assert keys == {"service", "network", "disk", "cpu"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,domain",
    [
        ("service", "service"),
        ("network", "network"),
        ("disk", "storage"),
        ("cpu", "os_host"),
    ],
)
async def test_each_button_injects_its_own_domain(app_ctx, scenario, domain):
    app, _r, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/simulate/scenario/{scenario}",
            json={"tenant_id": "default", "agent_id": "default_diag-test"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == domain
    assert body["trace_id"].startswith(f"sim-{scenario}-")

    env = _envelope(kafka)
    # Domain do kịch bản tự khai phải thắng suy đoán từ lane: SYS_HARD_FAIL →
    # lane_to_domain() trả "unknown", nếu để nó thắng thì service/network mất lĩnh vực.
    assert env["domain"] == domain
    assert env["evidence_source"] == "RemoteAgent"
    assert env["result"] == "FAILED"
    assert env["extracted_fact"]["result"] == "FAILED"
    assert kafka.sent[0][0] == "omni-diagnostic-evidence"


@pytest.mark.asyncio
async def test_unknown_scenario_400_lists_valid_keys(app_ctx):
    app, _r, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/scenario/bogus", json={})
    assert resp.status_code == 400
    assert "service" in resp.json()["detail"]
    assert kafka.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_lane", ["state", "resource", "app_log"])
async def test_proof_lane_names_are_not_simulator_lanes(app_ctx, proof_lane):
    """Hồi quy: đúng cú gọi cũ của portal, phải vẫn là 400 (không âm thầm hoạt động).

    Nếu ai đó thêm alias "state"/"resource" vào LANE_KEYS thì kịch bản sẽ chạy sai
    domain một cách im lặng — test này chặn đường đó.
    """
    app, _r, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/simulate/{proof_lane}", json={"target": "remote"})
    assert resp.status_code == 400
    assert kafka.sent == []


@pytest.mark.asyncio
async def test_scenario_marks_ingest_stage(app_ctx):
    app, redis, _k = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/scenario/disk", json={"tenant_id": "acme"})
    trace_id = resp.json()["trace_id"]
    keys = [k async for k in redis.scan_iter(match=f"*{trace_id}*")]
    assert keys, "INGEST stage phải được ghi để trace hiện ngay trên dashboard"


@pytest.mark.asyncio
async def test_agent_id_defaults_when_omitted(app_ctx):
    app, _r, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/simulate/scenario/cpu", json={})
    assert resp.status_code == 200
    agent_id = resp.json()["agent_id"]
    assert agent_id.startswith("sim-agent-")
    assert _envelope(kafka)["extracted_fact"]["agent_id"] == agent_id


@pytest.mark.asyncio
async def test_lane_route_still_works_unchanged(app_ctx):
    """Đường lane cũ không được đổi hành vi khi refactor sang _remote_envelope()."""
    app, _r, kafka = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/simulate/sys_resource", json={"target": "remote", "agent_id": "h1"}
        )
    assert resp.status_code == 200
    env = _envelope(kafka)
    assert env["lane"] == "SYS_RESOURCE"
    assert env["probe"] == "remote_system_metrics"
    assert env["evidence_source"] == "RemoteAgent"
