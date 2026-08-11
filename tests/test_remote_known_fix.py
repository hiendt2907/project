"""TDD cho workers.remote_known_fix — phản xạ nhanh cho remote-agent (VM
khách, không Prometheus). Trigger: deviation z-score/ngưỡng tĩnh đã tính sẵn
trong knowledge_pipeline. Thực thi: kênh lệnh bền (dispatch_if_eligible), vì
không có API trong-process nào để gọi thẳng một VM khách như proactive cluster
gọi K8s API."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from workers.remote_known_fix import try_remote_known_fix


class _FakeLLM:
    async def embed(self, model: str, input: str) -> dict:
        return {"embedding": [0.1] * 768}


class _FakePoint:
    def __init__(self, score: float, payload: dict[str, Any]):
        self.score = score
        self.payload = payload


class _FakeResponse:
    def __init__(self, points: list[_FakePoint]):
        self.points = points


class _FakeVectorStore:
    def __init__(self, points: list[_FakePoint]):
        self._points = points
        self.last_query_kwargs: dict[str, Any] = {}

    async def query_points(self, **kwargs: Any) -> _FakeResponse:
        self.last_query_kwargs = kwargs
        return _FakeResponse(self._points)


class _FakeKafka:
    async def send_dict(self, *a, **kw):
        pass


def _settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        memory_canonical_strip_pods=True,
        embed_model="nomic-embed-text:latest",
        omni_gateway_api_key="test-key",
        omni_gateway_internal_url="http://gateway.internal",
        kafka_topic_audit_chain="omni-audit-chain",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_no_candidate_returns_unresolved(fake_redis: FakeRedis) -> None:
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=_FakeVectorStore([]), kafka=_FakeKafka(),
    )
    result = await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=None,
        agent_id="agent-1", tenant_id="tenant-1", trace_id="trace-1",
    )
    assert result["resolved"] is False
    assert result["reason"] == "no_candidate"


@pytest.mark.asyncio
async def test_placeholder_candidate_never_reaches_dispatch(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lớp guard của known_fix_resolver phải chặn TRƯỚC khi chạm tới dispatch —
    nếu nó lọt qua, dispatch_if_eligible sẽ bị gọi với unit placeholder."""
    called = {"dispatch": False}

    async def _fake_dispatch(**kwargs: Any) -> dict:
        called["dispatch"] = True
        return {"dispatched": True, "reason": "dispatched", "command_id": "c1", "state": "ACCEPTED"}

    monkeypatch.setattr("workers.auto_recovery_bridge.dispatch_if_eligible", _fake_dispatch)

    points = [
        _FakePoint(
            0.9,
            {
                "exec_outcome": "success",
                "tool": "systemd.restart_unit",
                "auto_execute": True,
                "args": {"unit": "<valid_unit>"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=_FakeVectorStore(points), kafka=_FakeKafka(),
    )
    result = await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=None,
        agent_id="agent-1", tenant_id="tenant-1", trace_id="trace-1",
    )
    assert result["resolved"] is False
    assert result["reason"] == "placeholder_args"
    assert called["dispatch"] is False


@pytest.mark.asyncio
async def test_out_of_scope_candidate_never_reaches_dispatch(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"dispatch": False}

    async def _fake_dispatch(**kwargs: Any) -> dict:
        called["dispatch"] = True
        return {"dispatched": True, "reason": "dispatched", "command_id": "c1", "state": "ACCEPTED"}

    monkeypatch.setattr("workers.auto_recovery_bridge.dispatch_if_eligible", _fake_dispatch)

    points = [
        _FakePoint(
            0.9,
            {
                "exec_outcome": "success",
                "tool": "systemd.restart_unit",
                "auto_execute": True,
                "args": {"unit": "cryptominer"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=_FakeVectorStore(points), kafka=_FakeKafka(),
    )
    host_scope = frozenset({"nginx", "mysqld"})  # host này không chạy "cryptominer"
    result = await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=host_scope,
        agent_id="agent-1", tenant_id="tenant-1", trace_id="trace-1",
    )
    assert result["resolved"] is False
    assert result["reason"] == "out_of_host_scope"
    assert called["dispatch"] is False


@pytest.mark.asyncio
async def test_valid_candidate_dispatches_via_durable_channel(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ứng viên qua cả 2 lớp guard -> dispatch_if_eligible được gọi đúng shape
    {"suggested_recovery": {"capability", "unit"}, "confidence": ...}."""
    captured: dict[str, Any] = {}

    async def _fake_dispatch(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"dispatched": True, "reason": "dispatched", "command_id": "cmd-123", "state": "ACCEPTED"}

    monkeypatch.setattr("workers.auto_recovery_bridge.dispatch_if_eligible", _fake_dispatch)

    points = [
        _FakePoint(
            0.88,
            {
                "exec_outcome": "success",
                "tool": "systemd.restart_unit",
                "auto_execute": True,
                "args": {"unit": "nginx"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=_FakeVectorStore(points), kafka=_FakeKafka(),
    )
    host_scope = frozenset({"nginx"})
    result = await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=host_scope,
        agent_id="agent-1", tenant_id="tenant-1", trace_id="trace-1",
    )
    assert result["resolved"] is True
    assert result["command_id"] == "cmd-123"
    assert captured["final"]["suggested_recovery"] == {"capability": "systemd.restart_unit", "unit": "nginx"}
    assert captured["final"]["confidence"] == pytest.approx(0.88)
    assert captured["agent_id"] == "agent-1"
    assert captured["tenant_id"] == "tenant-1"
    assert captured["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_action_experience_search_scoped_by_tenant(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Đ55: action_experience trước đây là MỘT pool dùng chung mọi tenant —
    fix cho tenant A bị recall lại cho tenant B (chặn được nhờ ngưỡng
    confidence, không phải nhờ cách ly). Vector search phải nhắm đúng
    collection đã scope theo tenant_id, không phải collection unscoped."""
    async def _fake_dispatch(**kwargs: Any) -> dict:
        return {"dispatched": True, "reason": "dispatched", "command_id": "c1", "state": "ACCEPTED"}

    monkeypatch.setattr("workers.auto_recovery_bridge.dispatch_if_eligible", _fake_dispatch)
    points = [
        _FakePoint(0.88, {
            "exec_outcome": "success", "tool": "systemd.restart_unit",
            "auto_execute": True, "args": {"unit": "nginx"},
        })
    ]
    vs = _FakeVectorStore(points)
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=vs, kafka=_FakeKafka(),
    )
    await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=frozenset({"nginx"}),
        agent_id="agent-1", tenant_id="acme-corp", trace_id="trace-1",
    )
    assert vs.last_query_kwargs["collection_name"] == "action_experience:acme-corp"


@pytest.mark.asyncio
async def test_candidate_missing_unit_is_not_dispatched(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"dispatch": False}

    async def _fake_dispatch(**kwargs: Any) -> dict:
        called["dispatch"] = True
        return {"dispatched": True}

    monkeypatch.setattr("workers.auto_recovery_bridge.dispatch_if_eligible", _fake_dispatch)

    points = [
        _FakePoint(
            0.9,
            {
                "exec_outcome": "success",
                "tool": "systemd.restart_unit",
                "auto_execute": True,
                "args": {},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=_FakeVectorStore(points), kafka=_FakeKafka(),
    )
    result = await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=None,
        agent_id="agent-1", tenant_id="tenant-1", trace_id="trace-1",
    )
    assert result["resolved"] is False
    assert result["reason"] == "candidate_missing_unit"
    assert called["dispatch"] is False


@pytest.mark.asyncio
async def test_k8s_tool_candidate_never_matches_remote_capability_universe(
    fake_redis: FakeRedis,
) -> None:
    """`valid_tools` cho remote PHẢI là _SUPPORTED_CAPABILITIES, không phải
    TOOL_REGISTRY — một bản ghi gắn tool K8s không được coi là ứng viên hợp lệ
    cho remote-agent dù điểm giống cao."""
    points = [
        _FakePoint(
            0.95,
            {
                "exec_outcome": "success",
                "tool": "k8s_rollout_restart",
                "auto_execute": True,
                "args": {"deployment": "payment-api"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(),
        vector_store=_FakeVectorStore(points), kafka=_FakeKafka(),
    )
    result = await try_remote_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=None,
        agent_id="agent-1", tenant_id="tenant-1", trace_id="trace-1",
    )
    assert result["resolved"] is False
    assert result["reason"] == "no_candidate"
