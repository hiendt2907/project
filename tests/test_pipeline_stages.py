"""Unit tests for pipeline stage tracker and /trace/{id}/pipeline route."""
from __future__ import annotations

import json
import time

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from workers.pipeline_stages import PIPELINE_STAGES, mark_stage


# ── Helper ────────────────────────────────────────────────────────────────────

def _make_app(redis=None) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    from gateway.routes.trace import router
    app.include_router(router)
    return app


# ── mark_stage unit tests ─────────────────────────────────────────────────────

class TestMarkStage:
    async def test_writes_hash_field(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await mark_stage(redis, "trace-001", "EVIDENCE", "ok", detail="received")

        raw = await redis.hget("omni:trace:stages:trace-001", "EVIDENCE")
        assert raw is not None
        entry = json.loads(raw)
        assert entry["status"] == "ok"
        assert entry["detail"] == "received"
        assert entry["ts"] > 0

    async def test_sets_meta_started_at_once(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await mark_stage(redis, "trace-002", "EVIDENCE", "ok")
        raw1 = await redis.hget("omni:trace:stages:trace-002", "__meta__")
        meta1 = json.loads(raw1)
        first_started = meta1["started_at"]

        # Second call should preserve started_at
        await mark_stage(redis, "trace-002", "RAG", "ok")
        raw2 = await redis.hget("omni:trace:stages:trace-002", "__meta__")
        meta2 = json.loads(raw2)
        assert meta2["started_at"] == first_started

    async def test_multiple_stages_idempotent_ordering(self) -> None:
        redis = FakeRedis(decode_responses=True)
        for stage in ["INGEST", "EVIDENCE", "RAG", "LLM"]:
            await mark_stage(redis, "trace-003", stage, "ok")

        for stage in ["INGEST", "EVIDENCE", "RAG", "LLM"]:
            raw = await redis.hget("omni:trace:stages:trace-003", stage)
            assert raw is not None, f"Stage {stage} missing"
            entry = json.loads(raw)
            assert entry["status"] == "ok"

    async def test_invalid_stage_ignored(self) -> None:
        redis = FakeRedis(decode_responses=True)
        # Should not raise, and should not write
        await mark_stage(redis, "trace-004", "NONEXISTENT_STAGE", "ok")
        raw = await redis.hget("omni:trace:stages:trace-004", "NONEXISTENT_STAGE")
        assert raw is None

    async def test_empty_trace_id_ignored(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await mark_stage(redis, "", "EVIDENCE", "ok")  # should silently return

    async def test_long_trace_id_ignored(self) -> None:
        redis = FakeRedis(decode_responses=True)
        long_id = "x" * 200
        await mark_stage(redis, long_id, "EVIDENCE", "ok")
        raw = await redis.hget(f"omni:trace:stages:{long_id}", "EVIDENCE")
        assert raw is None

    async def test_sets_ttl(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await mark_stage(redis, "trace-ttl", "INGEST", "ok")
        ttl = await redis.ttl("omni:trace:stages:trace-ttl")
        assert ttl > 0

    async def test_domain_stored_in_meta(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await mark_stage(redis, "trace-domain", "EVIDENCE", "ok", domain="os_host")
        raw = await redis.hget("omni:trace:stages:trace-domain", "__meta__")
        meta = json.loads(raw)
        assert meta["domain"] == "os_host"


# ── /trace/{id}/pipeline route tests ─────────────────────────────────────────

class TestTracePipelineRoute:
    async def test_returns_404_when_not_found(self) -> None:
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/trace/no-such-trace/pipeline")
        assert resp.status_code == 404
        body = resp.json()
        assert body["found"] is False
        assert len(body["stages"]) == len(PIPELINE_STAGES)

    async def test_all_stages_present(self) -> None:
        redis = FakeRedis(decode_responses=True)
        trace_id = "trace-pipeline-test"
        now = time.time()
        # Seed a few stages
        for stage in ["INGEST", "EVIDENCE", "RAG"]:
            await mark_stage(redis, trace_id, stage, "ok")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/trace/{trace_id}/pipeline")

        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert len(body["stages"]) == len(PIPELINE_STAGES)
        stage_names = [s["stage"] for s in body["stages"]]
        assert stage_names == PIPELINE_STAGES
        assert "VERIFY" in stage_names

    async def test_pending_fill_for_unwritten_stages(self) -> None:
        redis = FakeRedis(decode_responses=True)
        trace_id = "trace-pending-fill"
        await mark_stage(redis, trace_id, "INGEST", "ok")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/trace/{trace_id}/pipeline")

        body = resp.json()
        stages_by_name = {s["stage"]: s for s in body["stages"]}

        assert stages_by_name["INGEST"]["status"] == "ok"
        # All other stages should be pending
        for stage in PIPELINE_STAGES:
            if stage != "INGEST":
                assert stages_by_name[stage]["status"] == "pending"
                assert stages_by_name[stage]["ts"] == 0
                assert stages_by_name[stage]["elapsed_ms"] == 0

    async def test_elapsed_ms_computed(self) -> None:
        redis = FakeRedis(decode_responses=True)
        trace_id = "trace-elapsed"

        # Manually set meta with a known started_at
        key = f"omni:trace:stages:{trace_id}"
        started_at = time.time() - 1.0  # 1 second ago
        stage_ts = started_at + 0.5     # 500ms after start
        meta = {"started_at": started_at, "updated_at": stage_ts, "trace_id": trace_id, "domain": ""}
        entry = {"status": "ok", "ts": stage_ts, "detail": ""}
        await redis.hset(key, "__meta__", json.dumps(meta))
        await redis.hset(key, "EVIDENCE", json.dumps(entry))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/trace/{trace_id}/pipeline")

        body = resp.json()
        stages_by_name = {s["stage"]: s for s in body["stages"]}
        elapsed = stages_by_name["EVIDENCE"]["elapsed_ms"]
        # Should be ~500ms (within 50ms tolerance)
        assert 450 <= elapsed <= 600, f"elapsed_ms={elapsed} unexpected"

    async def test_redis_none_returns_503(self) -> None:
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/trace/any-trace/pipeline")
        assert resp.status_code == 503

    async def test_verdict_from_dispatch_detail(self) -> None:
        redis = FakeRedis(decode_responses=True)
        trace_id = "trace-verdict"
        await mark_stage(redis, trace_id, "DISPATCH", "ok", detail="SUGGEST_REMEDIATION")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/trace/{trace_id}/pipeline")

        body = resp.json()
        assert body["verdict"] == "SUGGEST_REMEDIATION"


# ── /trace/purge route ────────────────────────────────────────────────────────

class TestTracePurgeRoute:
    async def test_purge_clears_stages_logs_advisory_and_event_stream(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await mark_stage(redis, "trace-purge-1", "EVIDENCE", "ok", detail="x")
        await mark_stage(redis, "trace-purge-2", "SCHEMA", "ok", detail="verdict=foo")
        await redis.rpush("omni:trace:logs:trace-purge-1", json.dumps({"line": "hi"}))
        await redis.set("omni:trace:advisory:trace-purge-1", json.dumps({"advisory": {}}))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/trace/purge")

        assert resp.status_code == 200
        body = resp.json()
        assert body["purged"] is True
        assert body["keys_deleted"] > 0

        assert await redis.exists("omni:trace:stages:trace-purge-1") == 0
        assert await redis.exists("omni:trace:stages:trace-purge-2") == 0
        assert await redis.exists("omni:trace:logs:trace-purge-1") == 0
        assert await redis.exists("omni:trace:advisory:trace-purge-1") == 0
        assert await redis.exists("omni:trace:events") == 0

    async def test_purge_leaves_evidence_cluster_dedup_state_untouched(self) -> None:
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:evcluster:seen:somefp", json.dumps({"total_count": 5}))
        await mark_stage(redis, "trace-purge-3", "EVIDENCE", "ok", detail="x")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/trace/purge")

        assert resp.status_code == 200
        assert await redis.exists("omni:evcluster:seen:somefp") == 1

    async def test_purge_redis_none_returns_503(self) -> None:
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/trace/purge")
        assert resp.status_code == 503

    async def test_purge_with_no_traces_returns_zero_deleted(self) -> None:
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/trace/purge")
        assert resp.status_code == 200
        assert resp.json()["keys_deleted"] == 0


# ── Chống drift giữa backend, nhãn portal, và E2E ────────────────────────────


class TestStageCountConsistencyAcrossLayers:
    """Ba nơi cùng phải biết số bước pipeline — lệch một nơi là drift âm thầm.

    Đã trả giá 2026-07-30: `AUTO_RECOVERY` được thêm vào `PIPELINE_STAGES` (13 bước)
    nhưng `STAGE_VI` của portal và `provider_overview.spec.ts` vẫn ở 12. E2E đỏ, và
    thoạt trông giống lỗi sản phẩm chứ không phải test lỗi thời — mất thời gian truy.
    Test đơn vị này đỏ TRƯỚC E2E (giây thay vì phút) và chỉ đúng chỗ cần sửa.
    """

    def _read(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_portal_stage_labels_cover_every_backend_stage(self) -> None:
        src = self._read("ui/apps/provider-portal/lib/pipeline.ts")
        block = src.split("export const STAGE_VI", 1)[1].split("};", 1)[0]
        missing = [s for s in PIPELINE_STAGES if f"{s}:" not in block]
        assert not missing, (
            f"STAGE_VI (portal) thieu nhan cho: {missing} — "
            f"nguoi dung se thay ma stage tho thay vi tieng Viet"
        )

    def test_e2e_stage_count_matches_backend(self) -> None:
        src = self._read("tests/e2e_portals/specs/provider_overview.spec.ts")
        marker = "const PIPELINE_STAGE_COUNT = "
        assert marker in src, "spec E2E khong con hang so PIPELINE_STAGE_COUNT"
        got = int(src.split(marker, 1)[1].split(";", 1)[0].strip())
        assert got == len(PIPELINE_STAGES), (
            f"E2E ky vong {got} buoc, backend co {len(PIPELINE_STAGES)} — "
            f"cap nhat PIPELINE_STAGE_COUNT trong provider_overview.spec.ts"
        )
