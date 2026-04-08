from __future__ import annotations

from types import SimpleNamespace

import pytest

from workers.selflearning_shadow import run_shadow_selflearning


class _DummyRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.calls.append((key, ttl, value))


@pytest.mark.asyncio
async def test_shadow_selflearning_noop_when_disabled() -> None:
    redis = _DummyRedis()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=False,
        ),
        redis=redis,
        ollama=None,
    )
    await run_shadow_selflearning(ctx, trace="t-1", sanitized_text="cpu high", machine={})
    assert redis.calls == []


@pytest.mark.asyncio
async def test_shadow_selflearning_stores_shadow_artifact() -> None:
    redis = _DummyRedis()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=True,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        ),
        redis=redis,
        ollama=None,
    )
    await run_shadow_selflearning(ctx, trace="t-2", sanitized_text="redis lag and timeout", machine={"hypothesis": "redis_backlog"})
    assert len(redis.calls) == 1
    key, ttl, val = redis.calls[0]
    assert key.startswith("omni:selflearn:shadow:t-2")
    assert ttl == 86400
    assert "redis_backlog" in val
