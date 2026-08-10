"""Đ49 S2 — agent_webhook.py fan-out domain=security FAILED evidence → omni-siem-raw.

plans/finguard-to-smart-siem-merge-2026-08-04.md phase S2: sau khi siem_bridge.py bị
retired (S0), đường vào omni-siem-raw duy nhất còn lại là gateway agent_webhook.py fan-out
1 item evidence ra 2 topic: omni-diagnostic-evidence (chẩn đoán, như mọi domain) +
omni-siem-raw (correlation, chỉ khi domain=security VÀ result=FAILED).
"""

from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(redis=None, kafka=None) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    app.state.kafka = kafka
    from gateway.routes.agent_webhook import router
    app.include_router(router)
    return app


class _TopicAwareKafkaCapture:
    def __init__(self):
        self.sent: list[tuple[str, bytes]] = []

    async def send_and_wait(self, topic: str, value: bytes):
        self.sent.append((topic, value))

    def decoded_on(self, topic: str) -> list[dict]:
        return [
            json.loads(json.loads(v)["data"])
            for t, v in self.sent
            if t == topic
        ]


def _security_evidence(
    probe: str = "security_auth_failures",
    result: str = "FAILED",
    trace_id: str = "trace-sec-001",
    extracted_fact: dict | None = None,
) -> dict:
    return {
        "trace_id": trace_id,
        "probe": probe,
        "result": result,
        "alert_hint": f"[host1] {probe} anomaly",
        "raw": "",
        "extracted_fact": extracted_fact or {
            "failed_login_count": 25,
            "distinct_users": 3,
            "normalized_entities": "user=root host=203.0.113.5 user=admin host=203.0.113.7",
        },
        "lane": "SIEM_SECURITY",
        "domain": "security",
        "alert_rule": "SecurityAuthFailureBurst",
    }


def _batch_request(evidence: list[dict], agent_id: str = "agent-1") -> dict:
    return {"agent_id": agent_id, "hostname": "10.210.14.1", "evidence": evidence}


@pytest.mark.asyncio
async def test_security_failed_evidence_fans_out_to_siem_raw():
    redis = FakeRedis(decode_responses=True)
    kafka = _TopicAwareKafkaCapture()
    app = _make_app(redis, kafka)
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.state.kafka_topic_siem_raw = "omni-siem-raw"

    body = _batch_request([_security_evidence()])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/webhook/agent/evidence", json=body)
    assert resp.status_code == 200
    assert resp.json()["enqueued"] == 1

    diag = kafka.decoded_on("omni-diagnostic-evidence")
    siem = kafka.decoded_on("omni-siem-raw")
    assert len(diag) == 1, "evidence chẩn đoán chuẩn vẫn phải nhận đủ (không thay thế)"
    assert len(siem) == 1, "domain=security + FAILED phải fan-out sang omni-siem-raw"

    incident = siem[0]
    assert incident["id"] == "trace-sec-001:security_auth_failures"
    assert incident["severity"] == "critical"  # 25 >= 20
    assert incident["category"] == "auth_failures"
    assert incident["source"] == "omni_siem"
    # INV_DATA_RESIDENCY: raw_log chỉ chứa chuỗi ĐÃ chuẩn hoá, không log thô
    assert incident["raw_log"] == "user=root host=203.0.113.5 user=admin host=203.0.113.7"


@pytest.mark.asyncio
async def test_security_passed_evidence_does_not_fan_out():
    """Không có gì bất thường (PASSED) → không cần correlation, không phải fan-out."""
    redis = FakeRedis(decode_responses=True)
    kafka = _TopicAwareKafkaCapture()
    app = _make_app(redis, kafka)
    app.state.kafka_topic_siem_raw = "omni-siem-raw"

    body = _batch_request([_security_evidence(result="PASSED")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/webhook/agent/evidence", json=body)
    assert resp.status_code == 200

    siem = kafka.decoded_on("omni-siem-raw")
    assert siem == []


@pytest.mark.asyncio
async def test_non_security_failed_evidence_does_not_fan_out():
    """Domain khác (vd application) dù FAILED cũng không đi omni-siem-raw."""
    redis = FakeRedis(decode_responses=True)
    kafka = _TopicAwareKafkaCapture()
    app = _make_app(redis, kafka)
    app.state.kafka_topic_siem_raw = "omni-siem-raw"

    body = _batch_request([{
        "trace_id": "trace-app-001",
        "probe": "remote_log_errors",
        "result": "FAILED",
        "alert_hint": "OOM kill: mysqld",
        "raw": "kernel: out of memory",
        "extracted_fact": {"failed_file_count": 5, "files_scanned": 10},
        "lane": "SYS_RESOURCE",
    }])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/webhook/agent/evidence", json=body)
    assert resp.status_code == 200

    siem = kafka.decoded_on("omni-siem-raw")
    assert siem == []


@pytest.mark.asyncio
async def test_domain_hint_from_collector_reaches_detect_domain():
    """Đ49 S2 gotcha đã vá: EvidenceItem.domain giờ được khai — domain_hint collector tự
    khai (build_envelope's "domain": SECURITY) phải thắng, không chỉ dựa vào prefix probe
    may mắn khớp. Test bằng 1 probe KHÔNG có prefix "security_" để chứng minh domain_hint
    thật sự có tác dụng, không phải trùng hợp tên probe."""
    redis = FakeRedis(decode_responses=True)
    kafka = _TopicAwareKafkaCapture()
    app = _make_app(redis, kafka)
    app.state.kafka_topic_siem_raw = "omni-siem-raw"

    body = _batch_request([{
        "trace_id": "trace-hint-001",
        "probe": "custom_probe_name",  # KHÔNG khớp bất kỳ prefix domain nào
        "result": "FAILED",
        "alert_hint": "anomaly",
        "raw": "",
        "extracted_fact": {"normalized_entities": "user=x host=y"},
        "lane": "SIEM_SECURITY",
        "domain": "security",  # domain_hint tường minh
        "alert_rule": "CustomSecurityRule",
    }])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/webhook/agent/evidence", json=body)
    assert resp.status_code == 200

    siem = kafka.decoded_on("omni-siem-raw")
    assert len(siem) == 1, "domain_hint tường minh phải thắng cascade đoán theo prefix"
