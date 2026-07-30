"""Computation-only coverage for src/rag, src/anomaly, src/ingest (no unittest.mock)."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from anomaly import forecast as fc
from anomaly import prophet_forecast as pf
from anomaly.three_sigma import ThreeSigmaGate, fingerprint_key_samples
from ingest.telegram import TelegramMessageSummary, summarize_message_update
from rag import error_ledger
from rag import redis_vector_store as rvs
from rag.redis_vector_store import EMBED_DIM, PointStruct, QueryResponse, RedisVectorStore
from rag.semantic_cache import SemanticCache
from rag.sop_ledger import (
    canonical_variant_key,
    sop_payload_for_fast_path,
    sop_point_id,
)
from rag.redis_vector_store import init_pg_pool


# --- Minimal async Redis fake for ThreeSigmaGate (no Mock) ---


class _ListPipeline:
    def __init__(self, parent: "_FakeAsyncRedis") -> None:
        self._p = parent
        self._ops: list[tuple[Any, ...]] = []

    def lpush(self, key: str, value: str) -> _ListPipeline:
        self._ops.append(("lpush", key, value))
        return self

    def ltrim(self, key: str, start: int, end: int) -> _ListPipeline:
        self._ops.append(("ltrim", key, start, end))
        return self

    def expire(self, key: str, ttl: int) -> _ListPipeline:
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> None:
        for op in self._ops:
            if op[0] == "lpush":
                _, key, val = op
                self._p._lists.setdefault(key, []).insert(0, val)
            elif op[0] == "ltrim":
                _, key, start, end = op
                lst = self._p._lists.get(key, [])
                self._p._lists[key] = lst[start : end + 1]
            elif op[0] == "expire":
                _, key, ttl = op
                self._p._ttls[key] = int(ttl)
        self._ops.clear()


class _FakeAsyncRedis:
    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self._ttls: dict[str, int] = {}

    def pipeline(self) -> _ListPipeline:
        return _ListPipeline(self)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start : end + 1]

    async def ttl(self, key: str) -> int:
        if key not in self._lists:
            return -2
        return self._ttls.get(key, -1)

    async def scan_iter(self, match: str = "*", count: int = 100) -> Any:
        del count  # API compatibility
        prefix = match[:-1] if match.endswith("*") else match
        for k in sorted(self._lists):
            if k.startswith(prefix):
                yield k


class _SemSearchResult:
    def __init__(self, docs: list[Any]) -> None:
        self.docs = docs


class _SemFTCommands:
    def __init__(self, parent: "_FakeSemRedis") -> None:
        self._p = parent

    async def info(self) -> dict[str, Any]:
        if self._p._info_ok:
            return {"ok": True}
        raise ConnectionError("no semcache index")

    async def create_index(self, *args: Any, **kwargs: Any) -> None:
        self._p._info_ok = True

    async def search(self, query: Any, query_params: Any = None) -> _SemSearchResult:
        return _SemSearchResult(list(self._p._search_docs))


class _FakeSemRedis:
    """Minimal Redis Stack stand-in for SemanticCache (no Mock)."""

    def __init__(self, *, search_docs: list[Any] | None = None, fail_set: bool = False) -> None:
        self._info_ok = False
        self._search_docs: list[Any] = list(search_docs or [])
        self._fail_set = fail_set
        self.json_sets: list[tuple[str, dict[str, Any]]] = []
        self._last_ttl: int | None = None

    def ft(self, name: str) -> _SemFTCommands:
        return _SemFTCommands(self)

    def json(self) -> _FakeSemRedis:
        return self

    async def set(self, key: str, path: str, doc: dict[str, Any]) -> None:
        if self._fail_set:
            raise RuntimeError("json set failed")
        self.json_sets.append((key, doc))

    async def expire(self, key: str, ttl: int) -> None:
        self._last_ttl = int(ttl)


class _SemFTInfoOk:
    async def info(self) -> dict[str, Any]:
        return {"ok": True}

    async def create_index(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("index should already exist")

    async def search(self, query: Any, query_params: Any = None) -> _SemSearchResult:
        return _SemSearchResult([])


class _FakeSemRedisInfoOk:
    def ft(self, name: str) -> _SemFTInfoOk:
        return _SemFTInfoOk()

    def json(self) -> _FakeSemRedisInfoOk:
        return self

    async def set(self, key: str, path: str, doc: dict[str, Any]) -> None:
        pass

    async def expire(self, key: str, ttl: int) -> None:
        pass


# --- anomaly.forecast ---


def test_linear_forecast_horizon_trend_and_low_confidence() -> None:
    y = np.linspace(0.0, 10.0, 5)
    pred, meta = fc.linear_forecast_horizon(y, horizon_steps=3, min_r_squared=0.99)
    assert pred.shape == (3,)
    assert meta["slope"] > 0
    noisy = [1.0, 5.0, 1.0, 6.0, 0.5]
    _, meta_noise = fc.linear_forecast_horizon(noisy, horizon_steps=2, min_r_squared=0.95)
    assert meta_noise["low_confidence"] is True


def test_linear_forecast_horizon_errors() -> None:
    with pytest.raises(ValueError):
        fc.linear_forecast_horizon([1.0], horizon_steps=1)


def test_oom_risk_from_series_paths() -> None:
    short = fc.oom_risk_from_series(
        [1e9, 2e9],
        total_ram_bytes=16e9,
        step_seconds=60.0,
        horizon_hours=1.0,
        kind="usage",
    )
    assert short["ok"] is False

    usage_vals = [4e9, 5e9, 6e9, 7e9, 8e9]
    out = fc.oom_risk_from_series(
        usage_vals,
        total_ram_bytes=16e9,
        step_seconds=300.0,
        horizon_hours=2.0,
        kind="usage",
        usage_warn_ratio=0.92,
    )
    assert out["ok"] is True
    assert "metric_kind" in out

    avail = [8e9, 7e9, 6e9, 5e9, 4e9]
    out2 = fc.oom_risk_from_series(
        avail,
        total_ram_bytes=16e9,
        step_seconds=300.0,
        horizon_hours=2.0,
        kind="available",
    )
    assert out2["ok"] is True

    noisy = [1.0, 5.0, 1.0, 6.0, 0.5]
    lowc = fc.oom_risk_from_series(
        noisy,
        total_ram_bytes=16e9,
        step_seconds=60.0,
        horizon_hours=1.0,
        kind="usage",
    )
    assert lowc.get("low_confidence") is True


def test_series_step_seconds_and_forecast_horizon_steps() -> None:
    assert fc.series_step_seconds([100.0]) == 300.0
    # Sorted [50,100,200] -> consecutive diffs [50,100] -> median 75
    assert fc.series_step_seconds([200.0, 100.0, 50.0]) == pytest.approx(75.0)
    assert fc.series_step_seconds([0.0, 50.0, 100.0, 150.0]) == pytest.approx(50.0)
    assert fc.series_step_seconds([10.0, 10.0, 10.0]) == 300.0

    assert fc.forecast_horizon_steps("30m", 60.0, cap=500) == 30
    assert fc.forecast_horizon_steps("2h", 3600.0, cap=10) == 2
    assert fc.forecast_horizon_steps("bad", 1.0, cap=5) == 5
    assert fc.forecast_horizon_steps("1.5h", 3600.0, cap=20) == 1
    assert fc.forecast_horizon_steps("2hx", 3600.0, cap=500) == 1
    assert fc.forecast_horizon_steps("45am", 60.0, cap=500) == 60
    assert fc.forecast_horizon_steps("xh", 3600.0, cap=10) == 1


def test_pandas_trend_forecast() -> None:
    pred, meta = fc.pandas_trend_forecast([1.0, 2.0, 4.0, 8.0], horizon_steps=2)
    assert len(pred) == 2
    assert meta["mean"] > 0
    assert "regression" in meta
    with pytest.raises(ValueError):
        fc.pandas_trend_forecast([1.0], horizon_steps=1)


def test_three_sigma_gate_constructor_validation() -> None:
    r = _FakeAsyncRedis()
    with pytest.raises(ValueError, match="window_size"):
        ThreeSigmaGate(r, window_size=2)
    with pytest.raises(ValueError, match="ttl_sec"):
        ThreeSigmaGate(r, ttl_sec=0)


# --- anomaly.prophet_forecast ---


def test_prophet_helpers_and_linear_fallback() -> None:
    assert pf.horizons_to_periods(1.0, "5m") == 12
    assert pf.horizons_to_periods(2.0, "30s") == 240
    assert pf.horizons_to_periods(0.5, "1h") == 1
    assert pf.step_to_pandas_freq("5m") == "5min"
    assert pf.step_to_pandas_freq("90s") == "90S"
    assert pf.step_to_pandas_freq("2h") == "2H"
    assert pf.step_to_pandas_freq("") == "5min"

    ts = pd.date_range("2024-01-01", periods=5, freq="5min", tz="UTC")
    df = pd.DataFrame({"ds": ts, "y": [1.0, 1.2, 1.1, 1.3, 1.4]})
    out, backend = pf.forecast_backend_used(df, periods=3)
    assert backend in ("prophet", "linear_fallback")
    assert len(out) == 3
    assert set(out.columns) >= {"ds", "yhat", "yhat_lower", "yhat_upper"}

    meta = pf.forecast_metric_meta(df, periods=2)
    assert "forecast" in meta and "backend" in meta
    assert len(pf.forecast_metric(df, periods=2)) == 2


def test_prophet_prepare_errors() -> None:
    with pytest.raises(ValueError):
        pf.forecast_metric(pd.DataFrame(), periods=1)
    bad = pd.DataFrame({"ds": [1, 2], "y": [float("nan"), float("nan")]})
    with pytest.raises(ValueError):
        pf.forecast_metric(bad, periods=1)


def test_step_to_pandas_freq_fallthrough() -> None:
    assert pf.step_to_pandas_freq("5min") == "5min"
    assert pf.step_to_pandas_freq("abc") == "5min"
    assert pf.step_to_pandas_freq("1x") == "5min"


def test_infer_freq_td_single_point() -> None:
    ts = pd.date_range("2024-01-01", periods=1, freq="5min", tz="UTC")
    td = pf._infer_freq_td(pd.Series(ts))
    assert td == pd.Timedelta(minutes=5)


def test_forecast_backend_with_mocked_prophet() -> None:
    from unittest.mock import MagicMock, patch
    import pandas as pd

    ts = pd.date_range("2024-01-01", periods=10, freq="5min", tz="UTC")
    df = pd.DataFrame({"ds": ts, "y": list(range(10))})

    future_ts = pd.date_range("2024-01-01 00:50", periods=13, freq="5min", tz="UTC")
    fake_fcst = pd.DataFrame({
        "ds": future_ts,
        "yhat": [float(i) for i in range(13)],
        "yhat_lower": [float(i) - 1 for i in range(13)],
        "yhat_upper": [float(i) + 1 for i in range(13)],
    })

    mock_prophet_instance = MagicMock()
    mock_prophet_instance.make_future_dataframe.return_value = fake_fcst
    mock_prophet_instance.predict.return_value = fake_fcst

    mock_prophet_cls = MagicMock(return_value=mock_prophet_instance)
    mock_prophet_module = MagicMock()
    mock_prophet_module.Prophet = mock_prophet_cls

    import sys
    with patch.dict(sys.modules, {"prophet": mock_prophet_module}):
        out, backend = pf.forecast_backend_used(df, periods=3)

    assert backend == "prophet"
    assert len(out) == 3


@pytest.mark.asyncio
async def test_three_sigma_gate_and_fingerprint() -> None:
    r = _FakeAsyncRedis()
    gate = ThreeSigmaGate(r, window_size=10, ttl_sec=60, key_prefix="t:")
    fp = fingerprint_key_samples("cpu", [1.0, 2.0])
    assert len(fp) == 16

    # Ten identical points → std≈0 → no z-score yet.
    for _ in range(10):
        await gate.observe("cpu.test", 1.0)
    is_a, z = await gate.observe("cpu.test", 1.0)
    assert is_a is False and z in (None, 0.0)
    is_spike, z_spike = await gate.observe("cpu.test", 600.0)
    assert is_spike is True and z_spike is not None and abs(z_spike) > 3.0

    ttl = await gate.ttl_for("cpu.test")
    assert ttl == 60
    n = await gate.key_count_estimate()
    assert n == 1


# --- rag.redis_vector_store pure paths ---


def test_ft_escape_stable_vec_embedding_response_docs_to_points() -> None:
    esc = rvs._ft_escape('foo-bar@test: "x"')
    assert "\\" in esc or "-" in esc

    v = rvs._stable_vec_from_text("hello-error", dim=32)
    assert len(v) == 32
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)

    emb = rvs._embedding_vector_from_response({"embedding": [0.1, 0.2]})
    assert emb == [0.1, 0.2]
    emb2 = rvs._embedding_vector_from_response({"embeddings": [[0.3, 0.4]]})
    assert emb2 == [0.3, 0.4]
    with pytest.raises(ValueError):
        rvs._embedding_vector_from_response({})

    assert rvs._is_embed_bad_request(ValueError("status code 400")) is True
    assert rvs._is_embed_bad_request(RuntimeError("nothing")) is False

    doc = SimpleNamespace(id="doc:col:abc", omni_payload='{"k":1}', __score=0.1)
    pts = rvs._docs_to_points([doc], score_threshold=0.5)
    assert len(pts) == 1 and pts[0].id == "abc"

    doc_bad = SimpleNamespace(id="x:y", omni_payload="not-json", __score=0.0)
    pts2 = rvs._docs_to_points([doc_bad], score_threshold=None)
    assert pts2[0].payload == {}


def test_rrf_merge_points() -> None:
    store = RedisVectorStore.__new__(RedisVectorStore)
    dense = [
        PointStruct(id="a", payload={"x": 1}, score=0.9),
        PointStruct(id="b", payload={}, score=0.7),
    ]
    sparse = [PointStruct(id="b", payload={"y": 2}, score=0.2)]
    merged = store._rrf_merge_points(dense, sparse, k=10, dense_weight=0.5)
    ids = [p.id for p in merged]
    assert "a" in ids and "b" in ids


@pytest.mark.asyncio
async def test_ensure_partition_invalid_name_raises() -> None:
    store = RedisVectorStore.__new__(RedisVectorStore)
    store._initialized_indexes = set()
    with pytest.raises(ValueError, match="invalid collection_name"):
        await store.ensure_partition_for_collection("9bad")


@pytest.mark.asyncio
async def test_init_pg_pool_raises_deprecation() -> None:
    with pytest.raises(DeprecationWarning):
        await init_pg_pool()


# --- rag.sop_ledger ---


def test_sop_ledger_pure() -> None:
    sid = sop_point_id(template_id="t1", variant_key="v1")
    assert sid == sop_point_id(template_id="t1", variant_key="v1")
    payload = sop_payload_for_fast_path(
        match_text="m" * 9000,
        tool="kubectl",
        args={"ns": "x"},
        auto_execute=False,
        template_id="tid",
        variant_key="vk" * 40,
    )
    assert len(payload["match_text"]) == 8000
    assert payload["auto_execute"] is False
    key = canonical_variant_key({"b": "2", "a": "1"})
    assert key == '{"a": "1", "b": "2"}'


# --- rag.error_ledger sanitize ---


def test_sanitize_ledger_text_redacts_bot_url() -> None:
    raw = "see https://api.telegram.org/botSECRET123/getMe for info"
    out = error_ledger._sanitize_ledger_text(raw)
    assert "[REDACTED]" in out
    assert "SECRET123" not in out


# --- ingest.telegram ---


def test_summarize_message_update_and_model() -> None:
    assert summarize_message_update({}) is None
    raw = {
        "update_id": 42,
        "message": {
            "message_id": 7,
            "chat": {"id": -100},
            "from": {"id": 99},
            "text": "hello",
        },
    }
    s = summarize_message_update(raw)
    assert isinstance(s, TelegramMessageSummary)
    assert s.update_id == 42 and s.text == "hello" and s.from_user_id == 99

    raw2 = {
        "update_id": 1,
        "edited_message": {"message_id": 2, "chat": {"id": 3}, "text": None},
    }
    s2 = summarize_message_update(raw2)
    assert s2 is not None and s2.from_user_id is None


# --- rag.semantic_cache ---


@pytest.mark.asyncio
async def test_semantic_cache_ensure_get_set() -> None:
    qr = QueryResponse(points=[PointStruct(id="p1", payload={"x": 1})])
    hit = SimpleNamespace(__score=0.01, result_json=qr.model_dump_json())
    r = _FakeSemRedis(search_docs=[hit])
    cache = SemanticCache(r, default_ttl_sec=120)
    await cache.ensure_ready()
    await cache.ensure_ready()
    vec = list(rvs._stable_vec_from_text("cache-key", dim=EMBED_DIM))
    got = await cache.get(vec, threshold=0.95)
    assert got is not None and got.points[0].id == "p1"
    await cache.set(vec, qr, ttl_sec=60)
    assert len(r.json_sets) == 1
    assert r._last_ttl == 60


@pytest.mark.asyncio
async def test_semantic_cache_get_miss_and_bad_vec() -> None:
    r = _FakeSemRedis(search_docs=[])
    cache = SemanticCache(r)
    await cache.ensure_ready()
    vec = list(rvs._stable_vec_from_text("k", dim=EMBED_DIM))
    assert await cache.get(vec) is None
    assert await cache.get([0.1, 0.2], threshold=0.5) is None


@pytest.mark.asyncio
async def test_semantic_cache_low_similarity_and_bad_json() -> None:
    bad_sim = SimpleNamespace(__score=0.5, result_json="{}")
    bad_json = SimpleNamespace(__score=0.01, result_json="not-json")
    vec = list(rvs._stable_vec_from_text("z", dim=EMBED_DIM))
    for docs in ([bad_sim], [bad_json]):
        r = _FakeSemRedis(search_docs=list(docs))
        cache = SemanticCache(r)
        await cache.ensure_ready()
        assert await cache.get(vec, threshold=0.95) is None


@pytest.mark.asyncio
async def test_semantic_cache_set_swallows_errors() -> None:
    r = _FakeSemRedis(fail_set=True)
    cache = SemanticCache(r)
    await cache.ensure_ready()
    vec = list(rvs._stable_vec_from_text("e", dim=EMBED_DIM))
    await cache.set(vec, QueryResponse(points=[]))


@pytest.mark.asyncio
async def test_semantic_cache_empty_result_json_field() -> None:
    empty_field = SimpleNamespace(__score=0.01, result_json="")
    r = _FakeSemRedis(search_docs=[empty_field])
    cache = SemanticCache(r)
    await cache.ensure_ready()
    vec = list(rvs._stable_vec_from_text("empty-json", dim=EMBED_DIM))
    assert await cache.get(vec, threshold=0.9) is None


@pytest.mark.asyncio
async def test_semantic_cache_set_triggers_lazy_ensure() -> None:
    r = _FakeSemRedis()
    cache = SemanticCache(r)
    vec = list(rvs._stable_vec_from_text("lazy", dim=EMBED_DIM))
    await cache.set(vec, QueryResponse(points=[]))
    assert r._info_ok is True
    assert len(r.json_sets) == 1


@pytest.mark.asyncio
async def test_semantic_cache_info_ok_path() -> None:
    r = _FakeSemRedisInfoOk()
    cache = SemanticCache(r)
    await cache.ensure_ready()
    vec = list(rvs._stable_vec_from_text("i", dim=EMBED_DIM))
    assert await cache.get(vec) is None


@pytest.mark.asyncio
async def test_semantic_cache_create_index_failure_keeps_not_ready() -> None:
    class _FTFail:
        async def info(self) -> None:
            raise ConnectionError("nope")

        async def create_index(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("create blocked")

        async def search(self, *a: Any, **k: Any) -> _SemSearchResult:
            return _SemSearchResult([])

    class _RFail:
        def ft(self, name: str) -> _FTFail:
            return _FTFail()

        def json(self) -> _RFail:
            return self

        async def set(self, *a: Any, **k: Any) -> None:
            pass

        async def expire(self, *a: Any, **k: Any) -> None:
            pass

    cache = SemanticCache(_RFail())
    await cache.ensure_ready()
    vec = list(rvs._stable_vec_from_text("f", dim=EMBED_DIM))
    assert await cache.get(vec) is None
