"""Coverage tests for gateway/routes/autonomy.py."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.autonomy import router


def _app(redis=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    return app


# ── POST/GET /autonomy/mutation ─────────────────────────────────────────────

def test_mutation_toggle_defaults_off_and_reports_master_kill_switch(monkeypatch):
    repo = SimpleNamespace(get_runtime_flag=AsyncMock(return_value=None))
    monkeypatch.setenv("OMNI_AUTO_EXECUTE_ENABLED", "false")
    with patch("gateway.routes.autonomy._get_admin_repo", return_value=repo):
        client = TestClient(_app(redis=AsyncMock()))
        resp = client.get("/autonomy/mutation", params={"tenant_id": "acme"})
    assert resp.status_code == 200
    assert resp.json()["requested"] is False
    assert resp.json()["effective"] is False
    assert resp.json()["reason"] == "tenant_toggle_off"


def test_mutation_toggle_requires_confirm_when_enabling(monkeypatch):
    repo = SimpleNamespace(get_runtime_flag=AsyncMock(return_value=False), set_runtime_flag=AsyncMock())
    monkeypatch.setenv("OMNI_AUTO_EXECUTE_ENABLED", "true")
    with patch("gateway.routes.autonomy._get_admin_repo", return_value=repo):
        client = TestClient(_app(redis=AsyncMock()))
        resp = client.post("/autonomy/mutation", json={
            "tenant_id": "acme", "enabled": True, "actor": "operator", "confirm": False,
        })
    assert resp.status_code == 409
    repo.set_runtime_flag.assert_not_awaited()


def test_mutation_toggle_enable_is_audited_but_global_kill_switch_still_wins(monkeypatch):
    repo = SimpleNamespace(get_runtime_flag=AsyncMock(return_value=False), set_runtime_flag=AsyncMock(return_value={"version": 2}))
    monkeypatch.setenv("OMNI_AUTO_EXECUTE_ENABLED", "false")
    with patch("gateway.routes.autonomy._get_admin_repo", return_value=repo):
        client = TestClient(_app(redis=AsyncMock()))
        resp = client.post("/autonomy/mutation", json={
            "tenant_id": "acme", "enabled": True, "actor": "operator", "confirm": True,
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["requested"] is True
    assert body["effective"] is False
    assert body["reason"] == "master_kill_switch_off"
    repo.set_runtime_flag.assert_awaited_once_with(
        flag_key="aoip_mutation_enabled", flag_value=True, value_type="bool",
        actor="operator", tenant_id="acme",
    )
