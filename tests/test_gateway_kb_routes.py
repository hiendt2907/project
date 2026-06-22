"""Tests for /kb — RAG knowledge base list/create/delete against Redis."""
from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(redis: FakeRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    from gateway.routes.kb import router

    app.include_router(router)
    return app


@pytest.fixture
def app_ctx():
    redis = FakeRedis(decode_responses=True)
    return _make_app(redis), redis


@pytest.mark.asyncio
async def test_list_kb_returns_200_even_without_ft(app_ctx):
    # FakeRedis has no RediSearch — list must degrade gracefully (empty, not 500).
    app, _redis = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/kb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert "counts" in body
    assert body["write_collection"] == "vendor_knowledge"


@pytest.mark.asyncio
async def test_create_kb_embeds_and_upserts(app_ctx, monkeypatch):
    app, redis = app_ctx

    async def _fake_embed(text, **kw):  # noqa: ANN001
        return [0.01] * 768

    monkeypatch.setattr("pkg.rag.ollama_embed.embed_text", _fake_embed)

    payload = {
        "title": "Container OOMKilled is a cgroup event",
        "knowledge": "OOMKilled means the container exceeded its cgroup memory limit, not node RAM.",
        "vendor": "Kubernetes",
        "category": "memory",
        "tier": "basic",
        "score": 88,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/kb", json=payload)
    assert resp.status_code == 201
    entry_id = resp.json()["id"]
    assert entry_id.startswith("kb-")

    raw = await redis.hget(f"doc:vendor_knowledge:{entry_id}", "omni_payload")
    assert raw is not None
    stored = json.loads(raw)
    assert stored["vendor"] == "Kubernetes"
    assert stored["score"] == 88
    assert stored["source"] == "kb_ui"


@pytest.mark.asyncio
async def test_create_kb_embed_failure_returns_502(app_ctx, monkeypatch):
    app, _redis = app_ctx

    async def _boom(text, **kw):  # noqa: ANN001
        raise RuntimeError("ollama down")

    monkeypatch.setattr("pkg.rag.ollama_embed.embed_text", _boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/kb", json={"title": "x title", "knowledge": "y" * 20})
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_create_kb_rejects_short_input(app_ctx):
    app, _redis = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/kb", json={"title": "ab", "knowledge": "short"})
    assert resp.status_code == 422  # pydantic min_length


@pytest.mark.asyncio
async def test_delete_kb(app_ctx):
    app, redis = app_ctx
    key = "doc:vendor_knowledge:kb-deadbeef0001"
    await redis.hset(key, mapping={"omni_payload": "{}"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ok = await c.delete("/kb/vendor_knowledge/kb-deadbeef0001")
        missing = await c.delete("/kb/vendor_knowledge/kb-doesnotexist")
    assert ok.status_code == 200
    assert missing.status_code == 404
    assert await redis.exists(key) == 0


@pytest.mark.asyncio
async def test_delete_kb_rejects_bad_id(app_ctx):
    app, _redis = app_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.delete("/kb/vendor_knowledge/bad%20id%2Fwith%2Fslash")
    assert resp.status_code in (400, 404)
