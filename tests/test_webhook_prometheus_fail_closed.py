"""P0 #1 — /webhook/prometheus phải fail-closed ở prod khi thiếu HMAC secret.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #1): mọi router
khác dùng `_require_api_key`, đã fail-closed 503 khi thiếu key ở prod. Endpoint này trước
đây chỉ log WARNING lúc khởi động khi thiếu OMNI_GATEWAY_WEBHOOK_SECRET rồi vẫn nhận request
bình thường (`_verify_hmac_signature` trả True vô điều kiện) — nếu operator quên set secret
ở prod, endpoint mở hoàn toàn ra Internet cho "Prometheus alert" giả.

Test gọi thẳng `_prometheus_webhook_body` (không khởi động lifespan/Kafka/Redis thật) với
Redis/Kafka giả tối thiểu, để kiểm đúng nhánh fail-closed mới mà không cần hạ tầng thật.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from starlette.requests import Request


def _http_scope(*, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhook/prometheus",
        "raw_path": b"/webhook/prometheus",
        "root_path": "",
        "query_string": b"",
        "headers": headers or [],
        "client": ("203.0.113.5", 50000),
        "server": ("testserver", 80),
    }


class _FakeRedisGetNone:
    async def get(self, _key: str) -> None:
        return None


def _make_request(body: bytes = b'{"receiver":"r","status":"firing","alerts":[]}') -> Request:
    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request(_http_scope(), receive=_receive)
    return req


@pytest.fixture(autouse=True)
def _restore_env():
    saved_mode = os.environ.get("OMNI_ENV_MODE")
    saved_secret = os.environ.get("OMNI_GATEWAY_WEBHOOK_SECRET")
    yield
    if saved_mode is not None:
        os.environ["OMNI_ENV_MODE"] = saved_mode
    else:
        os.environ.pop("OMNI_ENV_MODE", None)
    if saved_secret is not None:
        os.environ["OMNI_GATEWAY_WEBHOOK_SECRET"] = saved_secret
    else:
        os.environ.pop("OMNI_GATEWAY_WEBHOOK_SECRET", None)


@pytest.mark.asyncio
async def test_prod_without_webhook_secret_rejects_with_503(monkeypatch) -> None:
    from gateway import api as gw

    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", b"")
    monkeypatch.setattr(gw, "_redis", _FakeRedisGetNone())
    monkeypatch.setattr(gw, "_rate_tokens", 100)
    os.environ["OMNI_ENV_MODE"] = "prod"
    os.environ.pop("OMNI_GATEWAY_WEBHOOK_SECRET", None)

    req = _make_request()
    resp = await gw._prometheus_webhook_body(req, "test-trace-fail-closed")

    assert resp.status_code == 503
    body = resp.body.decode()
    assert "not configured" in body.lower() or "webhook secret" in body.lower()


@pytest.mark.asyncio
async def test_lab_mode_without_webhook_secret_still_open(monkeypatch) -> None:
    """Không phá hành vi lab hiện có: non-prod vẫn cho qua bước fail-closed mới (như trước)."""
    from gateway import api as gw

    sent: list[tuple[str, bytes]] = []

    class _FakeKafka:
        async def send_and_wait(self, topic: str, value: bytes) -> None:
            sent.append((topic, value))

    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", b"")
    monkeypatch.setattr(gw, "_redis", _FakeRedisGetNone())
    monkeypatch.setattr(gw, "_kafka", _FakeKafka())
    monkeypatch.setattr(gw, "_rate_tokens", 100)
    os.environ["OMNI_ENV_MODE"] = "lab"
    os.environ.pop("OMNI_GATEWAY_WEBHOOK_SECRET", None)

    req = _make_request()
    resp = await gw._prometheus_webhook_body(req, "test-trace-lab-open")

    assert resp.status_code == 200
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_prod_with_webhook_secret_configured_but_no_signature_rejects_401(monkeypatch) -> None:
    """Khi secret ĐÃ set ở prod, hành vi cũ (401 do thiếu chữ ký) vẫn giữ nguyên."""
    from gateway import api as gw

    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", b"real-secret")
    monkeypatch.setattr(gw, "_redis", _FakeRedisGetNone())
    monkeypatch.setattr(gw, "_rate_tokens", 100)
    os.environ["OMNI_ENV_MODE"] = "prod"

    req = _make_request()
    resp = await gw._prometheus_webhook_body(req, "test-trace-prod-nosig")

    assert resp.status_code == 401
