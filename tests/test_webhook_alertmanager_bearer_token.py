"""P0 #1 follow-up — Alertmanager nội bộ cần đường auth KHÁC HMAC.

Bối cảnh (2026-08-10, xảy ra thật ngay sau khi P0 #1 deploy lên GCP): fail-closed
chặn đúng ý (không còn mở webhook ra Internet khi thiếu secret), nhưng đồng thời
chặn luôn Alertmanager nội bộ (`k8s/chaos-test/alertmanager.yaml`, receiver
`omni-webhook`) — nó gửi self-monitoring/meta_self alert vào CHÍNH endpoint này,
và Alertmanager `webhook_configs` không có khả năng tự tính HMAC-SHA256 của body,
chỉ hỗ trợ `http_config.authorization` (Bearer token tĩnh). Fix: thêm cơ chế bearer
token `OMNI_ALERTMANAGER_WEBHOOK_TOKEN` làm đường auth thứ hai, song song HMAC.
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
        "client": ("10.42.0.5", 50000),
        "server": ("testserver", 80),
    }


def _req(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(_http_scope(headers=headers))


@pytest.fixture(autouse=True)
def _restore_env():
    saved = os.environ.get("OMNI_ALERTMANAGER_WEBHOOK_TOKEN")
    yield
    if saved is not None:
        os.environ["OMNI_ALERTMANAGER_WEBHOOK_TOKEN"] = saved
    else:
        os.environ.pop("OMNI_ALERTMANAGER_WEBHOOK_TOKEN", None)


def test_valid_bearer_token_passes_when_configured(monkeypatch) -> None:
    from gateway import api as gw

    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", b"")
    monkeypatch.setattr(gw, "_ALERTMANAGER_WEBHOOK_TOKEN", b"real-alertmanager-token")

    req = _req(headers=[(b"authorization", b"Bearer real-alertmanager-token")])
    assert gw._verify_webhook_auth(req, b"body") is True


def test_wrong_bearer_token_fails_when_configured(monkeypatch) -> None:
    from gateway import api as gw

    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", b"")
    monkeypatch.setattr(gw, "_ALERTMANAGER_WEBHOOK_TOKEN", b"real-alertmanager-token")

    req = _req(headers=[(b"authorization", b"Bearer wrong-token")])
    assert gw._verify_webhook_auth(req, b"body") is False


def test_missing_authorization_header_fails_when_token_configured(monkeypatch) -> None:
    from gateway import api as gw

    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", b"")
    monkeypatch.setattr(gw, "_ALERTMANAGER_WEBHOOK_TOKEN", b"real-alertmanager-token")

    req = _req(headers=[])
    assert gw._verify_webhook_auth(req, b"body") is False


def test_either_mechanism_alone_is_sufficient_hmac_side(monkeypatch) -> None:
    """Cả hai cơ chế cùng cấu hình: chỉ cần MỘT cái đúng, không cần cả hai."""
    import hashlib
    import hmac as _hmac_mod

    from gateway import api as gw

    secret = b"hmac-secret"
    monkeypatch.setattr(gw, "_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(gw, "_ALERTMANAGER_WEBHOOK_TOKEN", b"bearer-token")

    body = b'{"alerts":[]}'
    sig = _hmac_mod.new(secret, body, hashlib.sha256).hexdigest()
    req = _req(headers=[(b"x-hub-signature-256", f"sha256={sig}".encode())])
    assert gw._verify_webhook_auth(req, body) is True


def test_prod_fail_closed_check_considers_both_mechanisms() -> None:
    """Đúng chính bug thật: chỉ dùng not _WEBHOOK_SECRET sẽ chặn nhầm Alertmanager
    dù OMNI_ALERTMANAGER_WEBHOOK_TOKEN đã cấu hình. Đọc source để chặn regression
    (không cần dựng lại toàn bộ request/redis/kafka)."""
    src = open("src/gateway/api.py", encoding="utf-8").read()
    assert "not _WEBHOOK_SECRET\n            and not _ALERTMANAGER_WEBHOOK_TOKEN" in src
