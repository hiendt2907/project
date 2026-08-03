"""TDD cho pkg.reasoning.known_fix_resolver — sinh ra để vá lỗ thật production
2026-08-03: proactive dispatch `k8s_rollout_restart` với
`deployment='<valid_deployment>'` (placeholder chưa điền) chỉ vì điểm giống
vector ~0.7 vượt ngưỡng. Hai lớp kiểm mới, độc lập với điểm similarity:
placeholder trong args, và đối chiếu identifier với phạm vi host thật.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from pkg.reasoning.known_fix_resolver import (
    KnownFixResult,
    _has_placeholder,
    _out_of_scope,
    resolve_known_fix,
)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


def test_has_placeholder_detects_template_token():
    assert _has_placeholder("<valid_deployment>") is True
    assert _has_placeholder({"deployment": "<valid_deployment>", "ns": "prod"}) is True
    assert _has_placeholder(["ok", "<namespace>"]) is True


def test_has_placeholder_false_for_real_values():
    assert _has_placeholder("nginx-deployment") is False
    assert _has_placeholder({"deployment": "nginx", "ns": "prod"}) is False
    assert _has_placeholder({}) is False
    assert _has_placeholder(123) is False


def test_out_of_scope_none_when_no_host_scope_given():
    assert _out_of_scope({"unit": "anything.service"}, None) is None


def test_out_of_scope_flags_unknown_identifier():
    scope = frozenset({"nginx.service", "mysqld.service"})
    assert _out_of_scope({"unit": "cryptominer.service"}, scope) == "unit"


def test_out_of_scope_passes_known_identifier():
    scope = frozenset({"nginx.service"})
    assert _out_of_scope({"unit": "nginx.service"}, scope) is None


class _FakeLLM:
    async def embed(self, model: str, input: str) -> dict:
        return {"embedding": [0.1] * 768}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(memory_canonical_strip_pods=True, embed_model="nomic-embed-text:latest")


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

    async def query_points(self, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._points)


async def _fake_restart_tool(ctx: Any, args: dict) -> str:
    return f"restarted {args}"


@pytest.fixture(autouse=True)
def _patch_tool_registry(monkeypatch):
    monkeypatch.setattr(
        "workers.tools.TOOL_REGISTRY", {"systemd_restart_unit": _fake_restart_tool}, raising=False
    )
    from workers import tool_registry as tr_module

    class _AlwaysMissingRegistry:
        def has(self, name: str) -> bool:
            return False

    monkeypatch.setattr(tr_module, "get_tool_registry", lambda: _AlwaysMissingRegistry())


@pytest.mark.asyncio
async def test_rejects_placeholder_args_even_above_score_threshold(fake_redis: FakeRedis) -> None:
    """Sự cố production thật: điểm giống 0.79 vượt ngưỡng nhưng args là placeholder."""
    points = [
        _FakePoint(
            0.79,
            {
                "exec_outcome": "success",
                "tool": "systemd_restart_unit",
                "auto_execute": True,
                "args": {"unit": "<valid_unit>"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(), vector_store=_FakeVectorStore(points)
    )
    result = await resolve_known_fix(ctx, query_text="cpu cao", score_threshold=0.55)
    assert result.ok is False
    assert result.rejected_reason == "placeholder_args"


@pytest.mark.asyncio
async def test_rejects_args_outside_host_scope(fake_redis: FakeRedis) -> None:
    """Memory học trên host khác không được replay mù trên host hiện tại."""
    points = [
        _FakePoint(
            0.85,
            {
                "exec_outcome": "success",
                "tool": "systemd_restart_unit",
                "auto_execute": True,
                "args": {"unit": "nginx.service"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(), vector_store=_FakeVectorStore(points)
    )
    host_scope = frozenset({"mysqld.service"})  # host này không chạy nginx
    result = await resolve_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=host_scope
    )
    assert result.ok is False
    assert result.rejected_reason == "out_of_host_scope"


@pytest.mark.asyncio
async def test_executes_when_args_real_and_in_host_scope(fake_redis: FakeRedis) -> None:
    points = [
        _FakePoint(
            0.9,
            {
                "exec_outcome": "success",
                "tool": "systemd_restart_unit",
                "auto_execute": True,
                "args": {"unit": "nginx.service"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(), vector_store=_FakeVectorStore(points)
    )
    host_scope = frozenset({"nginx.service"})
    result = await resolve_known_fix(
        ctx, query_text="cpu cao", score_threshold=0.55, host_scope=host_scope
    )
    assert result.ok is True
    assert result.tool == "systemd_restart_unit"
    assert "nginx.service" in result.output


@pytest.mark.asyncio
async def test_falls_through_to_next_candidate_when_top1_rejected(fake_redis: FakeRedis) -> None:
    """Top-1 là rác (placeholder) không có nghĩa là bỏ cuộc — thử ứng viên kế."""
    points = [
        _FakePoint(
            0.9,
            {
                "exec_outcome": "success",
                "tool": "systemd_restart_unit",
                "auto_execute": True,
                "args": {"unit": "<valid_unit>"},
            },
        ),
        _FakePoint(
            0.8,
            {
                "exec_outcome": "success",
                "tool": "systemd_restart_unit",
                "auto_execute": True,
                "args": {"unit": "nginx.service"},
            },
        ),
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(), vector_store=_FakeVectorStore(points)
    )
    result = await resolve_known_fix(ctx, query_text="cpu cao", score_threshold=0.55)
    assert result.ok is True
    assert result.meta["args"] == {"unit": "nginx.service"}


@pytest.mark.asyncio
async def test_no_host_scope_skips_scope_check_but_still_blocks_placeholder(
    fake_redis: FakeRedis,
) -> None:
    """host_scope=None (proactive cluster chưa có cluster-inventory) tắt lớp (2),
    KHÔNG tắt lớp (1)."""
    points = [
        _FakePoint(
            0.9,
            {
                "exec_outcome": "success",
                "tool": "systemd_restart_unit",
                "auto_execute": True,
                "args": {"unit": "any.service"},
            },
        )
    ]
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(), vector_store=_FakeVectorStore(points)
    )
    result = await resolve_known_fix(ctx, query_text="cpu cao", score_threshold=0.55, host_scope=None)
    assert result.ok is True


@pytest.mark.asyncio
async def test_empty_points_returns_no_candidate(fake_redis: FakeRedis) -> None:
    ctx = SimpleNamespace(
        redis=fake_redis, settings=_settings(), llm=_FakeLLM(), vector_store=_FakeVectorStore([])
    )
    result = await resolve_known_fix(ctx, query_text="cpu cao", score_threshold=0.55)
    assert result.ok is False
    assert result.rejected_reason == "no_candidate"
