"""Gateway route coverage with fakeredis or hand-rolled async Redis fakes — no mocks."""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.agents import router as agents_router
from gateway.routes.compliance import router as compliance_router
from gateway.routes.kpi import router as kpi_router
from gateway.routes.playbooks import router as playbooks_router
from gateway.routes.siem import router as siem_router


@contextmanager
def _client_for(app: FastAPI):
    """TestClient must be used as a context manager for FastAPI lifespan to run."""
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


def test_cov_gateway_kpi_summary_with_counts():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        now = time.time()
        await r.zadd("omni:kpi:z:default:accepted", {"a": now - 100})
        await r.zadd("omni:kpi:z:default:rejected", {"b": now - 200})
        await r.zadd("omni:kpi:z:default:false_positive", {"c": now - 300})
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(kpi_router)
    with _client_for(app) as client:
        resp = client.get("/kpi/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["advisory"]["accepted"] == 1
        assert body["advisory"]["rejected"] == 1
        assert body["advisory"]["acceptance_rate"] is not None
        assert body["execution"]["false_positive_rate"] is not None


def test_cov_gateway_kpi_trend_windows():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        now = time.time()
        await r.zadd("omni:kpi:detected:default:SYS_RESOURCE", {"x": now - 10})
        await r.zadd("omni:kpi:resolved:default:SYS_RESOURCE", {"y": now - 5})
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(kpi_router)
    with _client_for(app) as client:
        for w in ("1h", "6h", "24h", "7d"):
            resp = client.get(f"/kpi/trend?window={w}")
            assert resp.status_code == 200
            assert "lanes" in resp.json()


def test_cov_gateway_kpi_no_redis_503():
    app = FastAPI()
    app.include_router(kpi_router)
    with _client_for(app) as client:
        assert client.get("/kpi/summary").status_code == 503


def test_cov_gateway_agents_list_ok():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        now = int(time.time())
        await r.set(
            "omni:agent:heartbeat:analyst",
            json.dumps({"updated_at": now, "status": "ok", "role": "analyst"}),
        )
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(agents_router)
    with _client_for(app) as client:
        resp = client.get("/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["overall"] == "ok"


def test_cov_gateway_agents_stale_and_unhealthy():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        old = int(time.time()) - 500
        await r.set(
            "omni:agent:heartbeat:core",
            json.dumps({"updated_at": old, "status": "degraded", "role": "core"}),
        )
        await r.set(
            "omni:agent:heartbeat:exec",
            json.dumps({"updated_at": int(time.time()), "status": "unhealthy", "role": "executor"}),
        )
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(agents_router)
    with _client_for(app) as client:
        assert client.get("/agents").json()["overall"] == "unhealthy"


class _AgentsBrokenRedis:
    async def keys(self, *_a: Any, **_kw: Any) -> list[str]:
        raise RuntimeError("redis unavailable")


def test_cov_gateway_agents_keys_error_503():
    app = FastAPI()
    app.include_router(agents_router)
    app.state.redis = _AgentsBrokenRedis()
    with _client_for(app) as client:
        assert client.get("/agents").status_code == 503


def test_cov_gateway_siem_overview():
    ts = datetime.now(timezone.utc).isoformat()
    block = {
        "seq": 1,
        "event_type": "ADVISORY_DECISION",
        "trace_id": "t-siem",
        "timestamp_utc": ts,
        "payload": {"verdict": "SUGGEST", "root_cause": "x" * 200, "affected_workload": "ns/d"},
        "block_hash": "h" * 32,
    }
    bad_ts_block = {
        "seq": 2,
        "event_type": "HITL_DECISION",
        "trace_id": "t2",
        "timestamp_utc": "not-a-date",
        "payload": {"verdict": "UNKNOWN"},
        "block_hash": "b2",
    }
    raw_bad = "{not-json"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("audit_chain:seq", "2")
        await r.set("audit_chain:head_hash", "a" * 64)
        await r.rpush(
            "audit_chain:blocks",
            json.dumps(block),
            json.dumps(bad_ts_block),
            raw_bad,
        )
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(siem_router)
    with _client_for(app) as client:
        resp = client.get("/siem/overview?limit=10")
        assert resp.status_code == 200
        out = resp.json()
        assert out["chain"]["total_blocks"] == 2
        assert len(out["recent_blocks"]) >= 1


def test_cov_gateway_siem_redis_get_error_503():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        inner = fakeredis.aioredis.FakeRedis(decode_responses=True)

        class _BadGetRedis:
            async def get(self, *_a: Any, **_kw: Any) -> None:
                raise RuntimeError("get failed")

            async def lrange(self, *_a: Any, **_kw: Any) -> list[str]:
                return await inner.lrange(*_a, **_kw)

        app.state.redis = _BadGetRedis()
        yield
        await inner.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(siem_router)
    with _client_for(app) as client:
        assert client.get("/siem/overview").status_code == 503


def test_cov_gateway_compliance_export_and_stats():
    ts = datetime.now(timezone.utc).isoformat()
    b1 = {
        "seq": 1,
        "event_type": "ADVISORY_DECISION",
        "trace_id": "c1",
        "timestamp_utc": ts,
        "tenant_id": "default",
        "block_hash": "hash1",
        "prev_hash": "0" * 64,
        "signature_hex": None,
    }
    b2 = {
        "seq": 2,
        "event_type": "ADVISORY_DISPATCHED",
        "trace_id": "c2",
        "timestamp_utc": ts,
        "tenant_id": "default",
        "block_hash": "hash2",
        "prev_hash": "wrong-prev",
        "signature_hex": "sig",
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.rpush("audit_chain:blocks", json.dumps(b1), json.dumps(b2))
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(compliance_router)
    with _client_for(app) as client:
        csv_resp = client.get("/crat/export?format=csv&days=30")
        assert csv_resp.status_code == 200
        assert "text/csv" in csv_resp.headers.get("content-type", "")
        rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
        assert len(rows) == 2

        jresp = client.get("/crat/export?format=json&tenant_id=default&days=30")
        assert jresp.status_code == 200
        assert jresp.json()["total"] == 2

        stats = client.get("/crat/stats?tenant_id=default")
        assert stats.status_code == 200
        st = stats.json()
        assert st["total_blocks"] == 2
        assert st["chain_valid"] is False
        assert st["has_signature"] is True


def test_cov_gateway_compliance_tenant_key():
    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "seq": 1,
        "event_type": "ADVISORY_DECISION",
        "trace_id": "t1",
        "timestamp_utc": ts,
        "tenant_id": "acme",
        "block_hash": "h1",
        "prev_hash": "0" * 64,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.rpush("audit_chain:acme:blocks", json.dumps(row))
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(compliance_router)
    with _client_for(app) as client:
        r = client.get("/crat/stats?tenant_id=acme")
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "acme"


def test_cov_gateway_compliance_filters_old_blocks():
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.rpush(
            "audit_chain:blocks",
            json.dumps(
                {
                    "seq": 1,
                    "event_type": "X",
                    "trace_id": "o",
                    "timestamp_utc": old_ts,
                    "block_hash": "a",
                    "prev_hash": "0" * 64,
                }
            ),
            json.dumps(
                {
                    "seq": 2,
                    "event_type": "Y",
                    "trace_id": "n",
                    "timestamp_utc": new_ts,
                    "block_hash": "b",
                    "prev_hash": "a",
                }
            ),
        )
        app.state.redis = r
        yield
        await r.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(compliance_router)
    with _client_for(app) as client:
        data = client.get("/crat/export?format=json&days=30").json()
        assert data["total"] == 1


class PlaybookJsonRedis:
    """Minimal async Redis subset for gateway playbooks routes (fakeredis has no JSON.*)."""

    def __init__(self, docs: dict[str, dict], states: dict[str, str]) -> None:
        self._docs = docs
        self._states = states

    async def keys(self, pattern: str) -> list[str]:
        prefix = pattern.rstrip("*")
        return sorted(k for k in self._docs if k.startswith(prefix))

    async def execute_command(self, *args: Any) -> str | None:
        if not args:
            return None
        if str(args[0]).upper() != "JSON.GET":
            raise RuntimeError("only JSON.GET supported in test fake")
        key = str(args[1])
        doc = self._docs.get(key)
        return json.dumps(doc) if doc else None

    async def get(self, key: str) -> str | None:
        return self._states.get(key)


def test_cov_gateway_playbooks_list_get_state():
    doc = {"playbook_id": "pb1", "name": "Example"}
    redis = PlaybookJsonRedis({"pb:pb1": doc}, {"omni:playbook:state:t1:pb1": json.dumps({"step": 2})})
    app = FastAPI()
    app.include_router(playbooks_router)
    app.state.redis = redis
    with _client_for(app) as client:
        r = client.get("/playbooks")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert client.get("/playbooks/pb1").status_code == 200
        assert client.get("/playbooks/missing").status_code == 404
        st = client.get("/playbooks/pb1/state?trace_id=t1")
        assert st.status_code == 200
        assert st.json()["step"] == 2
        assert client.get("/playbooks/pb1/state?trace_id=nope").status_code == 404


def test_cov_gateway_playbooks_list_skips_bad_json_value():
    redis = PlaybookJsonRedis({"pb:pb1": {"playbook_id": "pb1"}}, {})
    bad_redis = _PlaybookKeysWithBadSecond(redis)
    app = FastAPI()
    app.include_router(playbooks_router)
    app.state.redis = bad_redis
    with _client_for(app) as client:
        resp = client.get("/playbooks")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


class _PlaybookKeysWithBadSecond:
    def __init__(self, inner: PlaybookJsonRedis) -> None:
        self._inner = inner

    async def keys(self, pattern: str) -> list[str]:
        base = await self._inner.keys(pattern)
        return [*base, "pb:broken"]

    async def execute_command(self, *args: Any) -> str | None:
        if len(args) >= 2 and str(args[1]) == "pb:broken":
            return "not-json{"
        return await self._inner.execute_command(*args)

    async def get(self, key: str) -> str | None:
        return await self._inner.get(key)
