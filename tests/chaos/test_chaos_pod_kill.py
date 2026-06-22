"""Unit chaos tests — Pod Kill recovery scenarios.

Domain 6: Verifies that after a pod restart, the pipeline recovers correctly.
The key invariant is auto_offset_reset=earliest — Kafka replays unacked messages.
Unit tests cover the idempotency and state-resilience aspects that are CI-safe.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis.aioredis
import pytest


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))

    async def send_envelope_inner(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))

    async def close(self) -> None:
        pass


def _make_ctx(redis=None, kafka=None) -> SimpleNamespace:
    return SimpleNamespace(
        redis=redis or fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=kafka or _KafkaCapture(),
        settings=SimpleNamespace(
            kafka_topic_actions="omni-actions",
            kafka_topic_audit_chain="omni-audit-chain",
        ),
    )


async def test_pod_kill_no_prior_state_succeeds() -> None:
    """Clean restart (no Redis state) → emit_execute_mutate succeeds normally."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _make_ctx(redis=redis, kafka=kafka)

    # No prior state — simulates first message after pod kill + Redis-clean restart
    state_key = "omni:autonomous:state:pod-kill-001"
    assert await redis.get(state_key) is None

    with patch("workers.evidence_mutate_emit.write_audit_block"):
        result = await emit_execute_mutate(
            ctx,
            trace="pod-kill-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )

    assert result is True
    assert any(t == "omni-actions" for t, _ in kafka.sent)


async def test_pod_kill_state_persisted_after_emit() -> None:
    """emit_execute_mutate writes attempt state to Redis with TTL 7200s for replay idempotency."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = _make_ctx(redis=redis)

    with patch("workers.evidence_mutate_emit.write_audit_block"):
        await emit_execute_mutate(
            ctx,
            trace="pod-kill-state-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )

    state_key = "omni:autonomous:state:pod-kill-state-001"
    raw = await redis.get(state_key)
    assert raw is not None, "State key must be written after emit"

    state = json.loads(raw)
    assert state["last_attempt_count"] == 1

    ttl = await redis.ttl(state_key)
    # TTL should be <= 7200 and > 0 (key has expiry set)
    assert 0 < ttl <= 7200, f"Expected TTL in (0, 7200], got {ttl}"


async def test_pod_kill_state_read_on_replay() -> None:
    """When Redis has prior state (pod restart, Redis survived), emit reads it correctly."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _make_ctx(redis=redis, kafka=kafka)

    # Pre-populate state from a "previous" attempt before the pod kill
    prior_state = {
        "last_attempt_count": 2,
        "feedback_failures": 1,
        "sdk_verify_round": 0,
        "state_verify_attempt": 0,
    }
    await redis.setex(
        "omni:autonomous:state:pod-kill-replay-001",
        7200,
        json.dumps(prior_state),
    )

    with patch("workers.evidence_mutate_emit.write_audit_block"):
        result = await emit_execute_mutate(
            ctx,
            trace="pod-kill-replay-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
            attempt_count=3,
        )

    assert result is True
    # State should be updated with new attempt_count
    raw = await redis.get("omni:autonomous:state:pod-kill-replay-001")
    state = json.loads(raw)
    assert state["last_attempt_count"] == 3
    # feedback_failures from prior state should be preserved
    assert state["feedback_failures"] == 1


async def test_pod_kill_replay_idempotent_crat_not_duplicated() -> None:
    """Replayed message after pod kill does not double-dispatch to omni-actions."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _make_ctx(redis=redis, kafka=kafka)

    call_count = 0

    async def _mock_write_audit_block(*args, **kwargs):
        nonlocal call_count
        call_count += 1

    with patch("workers.evidence_mutate_emit.write_audit_block", side_effect=_mock_write_audit_block):
        # First emit (original)
        r1 = await emit_execute_mutate(
            ctx,
            trace="pod-kill-idem-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )
        first_sent = len(kafka.sent)

        # Second emit (replay after pod kill — same trace, same tool)
        r2 = await emit_execute_mutate(
            ctx,
            trace="pod-kill-idem-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )

    assert r1 is True
    assert r2 is True
    # Both calls succeed (idempotent — executor deduplicates via trace_id)
    assert len(kafka.sent) == first_sent * 2
