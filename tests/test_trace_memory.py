"""Unit tests for Redis-backed trace memory (blackboard)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from workers.memory.initial_symptom import InitialSymptom
from workers.memory.trace_memory import (
    OmniTraceMemory,
    REDIS_KEY_TEMPLATE,
    TTL_SEC,
    format_trace_memory_block,
    load_trace_memory,
    save_trace_memory,
    truncate_for_action_record,
)
from workers.memory.trace_memory import ActionRecord


def test_truncate_for_action_record():
    long = "x" * 6000
    t = truncate_for_action_record(long)
    assert len(t) < len(long)
    assert "[TRUNCATED orig_len=6000]" in t


def test_render_llm_context_bounded():
    mem = OmniTraceMemory(
        trace_id="t1",
        initial_symptoms="sym",
        action_history=[
            ActionRecord(
                step=1,
                tool_name="k8s_describe_resource",
                args={"namespace": "ns"},
                result_summary="ok",
                kind="readonly_executed",
            )
        ],
    )
    s = mem.render_llm_context()
    assert "k8s_describe_resource" in s
    assert "Initial symptoms" in s


def test_format_trace_memory_block_xml():
    mem = OmniTraceMemory(
        trace_id="t2",
        working_hypothesis="H1 & H2",
        action_history=[],
    )
    block = format_trace_memory_block(mem)
    assert "<TRACE_MEMORY>" in block
    assert "<HYPOTHESIS>" in block
    assert "H1" in block
    assert "&amp;" in block  # XML escape of &
    assert "<HISTORY>" in block


def test_format_trace_memory_includes_initial_symptom():
    mem = OmniTraceMemory(
        trace_id="t3",
        initial_symptom=InitialSymptom(alertname="PodDown", namespace="ns"),
        action_history=[],
    )
    block = format_trace_memory_block(mem)
    assert "<INITIAL_SYMPTOM>" in block
    assert "PodDown" in block


@pytest.mark.asyncio
async def test_load_save_roundtrip():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()

    m0 = await load_trace_memory(redis, "tr-1", initial_symptoms="alert text", seed_attempt=0)
    assert m0.trace_id == "tr-1"
    assert "alert" in m0.initial_symptoms

    m0.working_hypothesis = "updated"
    m0.action_history.append(
        ActionRecord(
            step=1,
            tool_name="t",
            args={},
            result_summary="r",
            kind="readonly_executed",
        )
    )
    await save_trace_memory(redis, m0)

    redis.setex.assert_called_once()
    key, ttl, raw = redis.setex.call_args[0]
    assert ttl == TTL_SEC
    assert key == REDIS_KEY_TEMPLATE.format(trace_id="tr-1")
    assert "updated" in raw

    redis.get = AsyncMock(return_value=raw)
    m1 = await load_trace_memory(redis, "tr-1", initial_symptoms="fallback", seed_attempt=0)
    assert m1.working_hypothesis == "updated"
    assert len(m1.action_history) == 1


@pytest.mark.asyncio
async def test_load_trace_memory_none_redis():
    m = await load_trace_memory(None, "x", initial_symptoms="s", seed_attempt=0)
    assert m.trace_id == "x"
    await save_trace_memory(None, m)  # no-op


@pytest.mark.asyncio
async def test_load_trace_memory_merges_initial_symptom():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    sym = InitialSymptom(alertname="FromBatch", namespace="n1")
    m0 = await load_trace_memory(redis, "tr-sym", initial_symptoms="text", initial_symptom=sym, seed_attempt=0)
    assert m0.initial_symptom is not None
    assert m0.initial_symptom.alertname == "FromBatch"
    await save_trace_memory(redis, m0)
    raw = redis.setex.call_args[0][2]
    redis.get = AsyncMock(return_value=raw)
    m1 = await load_trace_memory(redis, "tr-sym", initial_symptoms="text", initial_symptom=None, seed_attempt=0)
    assert m1.initial_symptom is not None
    assert m1.initial_symptom.alertname == "FromBatch"
