"""Coverage tests for gateway/routes/autonomy.py."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.autonomy import router
from pkg.autonomy.policy import PolicyRule, AutonomyLevel


def _app(redis=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    return app


def _rule() -> PolicyRule:
    return PolicyRule(lane="SYS_RESOURCE", severity="high", action_type="restart", level=AutonomyLevel.SUGGEST_ONLY)


# ── GET /autonomy/policy ──────────────────────────────────────────────────────

def test_get_policy_no_redis():
    client = TestClient(_app(redis=None))
    resp = client.get("/autonomy/policy")
    assert resp.status_code == 503


def test_get_policy_success():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.get_policy", return_value=[_rule()]):
        client = TestClient(_app(redis=mock_redis))
        resp = client.get("/autonomy/policy")
    assert resp.status_code == 200
    data = resp.json()
    assert "policy" in data


def test_get_policy_store_raises():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.get_policy", side_effect=RuntimeError("store fail")):
        client = TestClient(_app(redis=mock_redis))
        resp = client.get("/autonomy/policy")
    assert resp.status_code == 500


# ── POST /autonomy/policy/rule ────────────────────────────────────────────────

_VALID_RULE = {"lane": "SYS_RESOURCE", "severity": "high", "action_type": "restart", "level": "SUGGEST_ONLY"}


def test_add_policy_rule_no_redis():
    client = TestClient(_app(redis=None))
    resp = client.post("/autonomy/policy/rule", json=_VALID_RULE)
    assert resp.status_code == 503


def test_add_policy_rule_success():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.set_rule", new_callable=AsyncMock):
        client = TestClient(_app(redis=mock_redis))
        resp = client.post("/autonomy/policy/rule", json=_VALID_RULE)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_add_policy_rule_store_raises():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.set_rule", side_effect=RuntimeError("fail")):
        client = TestClient(_app(redis=mock_redis))
        resp = client.post("/autonomy/policy/rule", json=_VALID_RULE)
    assert resp.status_code == 500


# ── GET /autonomy/policy/history ──────────────────────────────────────────────

def test_get_policy_history_no_redis():
    client = TestClient(_app(redis=None))
    resp = client.get("/autonomy/policy/history")
    assert resp.status_code == 503


def test_get_policy_history_success():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.get_history", return_value=[{"action": "suggest"}]):
        client = TestClient(_app(redis=mock_redis))
        resp = client.get("/autonomy/policy/history")
    assert resp.status_code == 200
    assert "history" in resp.json()


def test_get_policy_history_raises():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.get_history", side_effect=RuntimeError("err")):
        client = TestClient(_app(redis=mock_redis))
        resp = client.get("/autonomy/policy/history")
    assert resp.status_code == 500


# ── POST /autonomy/policy/reset ───────────────────────────────────────────────

def test_reset_policy_no_redis():
    client = TestClient(_app(redis=None))
    resp = client.post("/autonomy/policy/reset")
    assert resp.status_code == 503


def test_reset_policy_success():
    mock_redis = AsyncMock()
    with (
        patch("gateway.routes.autonomy._store.reset_to_defaults", new_callable=AsyncMock),
        patch("gateway.routes.autonomy._store.get_policy", return_value=[]),
    ):
        client = TestClient(_app(redis=mock_redis))
        resp = client.post("/autonomy/policy/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reset"


def test_reset_policy_raises():
    mock_redis = AsyncMock()
    with patch("gateway.routes.autonomy._store.reset_to_defaults", side_effect=RuntimeError("fail")):
        client = TestClient(_app(redis=mock_redis))
        resp = client.post("/autonomy/policy/reset")
    assert resp.status_code == 500
