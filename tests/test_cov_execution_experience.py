"""Coverage tests for execution.experience module."""
from __future__ import annotations

import os
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")

from execution.experience import (
    truncate_lesson_to_budget,
    _embedding_from_response,
    SandboxLessonInput,
    synthesize_lesson_text,
    routing_experience_point_id,
    upsert_action_experience,
    record_routing_from_success,
    record_agent_playbook_from_trajectory,
    record_routing_exhausted_no_data,
    record_sandbox_lesson,
    fetch_action_experience_context,
)
from rag.redis_vector_store import EMBED_DIM, PointStruct, QueryResponse
from workers.routing_policy import (
    ROUTING_SOURCE_SLOW_PATH,
    ROUTING_SOURCE_AGENT_SESSION,
    ROUTING_SOURCE_SLOW_PATH_EXHAUSTED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides):
    from workers.settings import WorkerSettings
    ws = WorkerSettings()
    for k, v in overrides.items():
        object.__setattr__(ws, k, v)
    return ws


def _fake_vec():
    return [0.1] * EMBED_DIM


def _make_ctx(*, settings=None, enabled=True, action_enabled=True, routing_enabled=True):
    ws = settings or _make_settings()
    ws.action_experience_enabled = action_enabled
    ws.routing_experience_enabled = routing_enabled
    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": _fake_vec()})
    llm.chat = AsyncMock(return_value={"message": {"content": "bài học tốt exit=0"}})
    vs = MagicMock()
    vs.upsert = AsyncMock()
    vs.query_points = AsyncMock(return_value=QueryResponse(points=[]))
    sem = MagicMock()
    sem.acquire = AsyncMock(return_value="token")
    sem.release = AsyncMock()
    ctx = SimpleNamespace(
        settings=ws,
        llm=llm,
        vector_store=vs,
        semaphore=sem,
        inbound_user_text="redis pod crash in namespace prod",
        inbound_trace_id="trace-001",
        llm_slot_held=False,
    )
    return ctx


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

def test_truncate_lesson_no_truncation():
    assert truncate_lesson_to_budget("hello", 100) == "hello"


def test_truncate_lesson_truncates():
    result = truncate_lesson_to_budget("a" * 200, 50)
    assert result.endswith("...")
    assert len(result) == 50


def test_truncate_lesson_strips_newlines():
    result = truncate_lesson_to_budget("line1\nline2", 100)
    assert "\n" not in result


def test_truncate_lesson_empty():
    assert truncate_lesson_to_budget("", 100) == ""


def test_embedding_from_response_embedding_key():
    result = _embedding_from_response({"embedding": [0.1, 0.2]})
    assert result == [0.1, 0.2]


def test_embedding_from_response_embeddings_key():
    result = _embedding_from_response({"embeddings": [[0.3, 0.4]]})
    assert result == [0.3, 0.4]


def test_embedding_from_response_missing_raises():
    with pytest.raises(ValueError):
        _embedding_from_response({})


def test_routing_experience_point_id_stable():
    id1 = routing_experience_point_id("pod crash", "kubectl", {"ns": "prod"})
    id2 = routing_experience_point_id("pod crash", "kubectl", {"ns": "prod"})
    assert id1 == id2


def test_routing_experience_point_id_differs_on_tool():
    id1 = routing_experience_point_id("pod crash", "kubectl", {})
    id2 = routing_experience_point_id("pod crash", "helm", {})
    assert id1 != id2


# ---------------------------------------------------------------------------
# synthesize_lesson_text tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesize_lesson_policy_blocked():
    ws = _make_settings()
    llm = MagicMock()
    inp = SandboxLessonInput(
        trace_id="trace-one", run_id="r1", command="rm -rf /", exit_code=-2,
        stdout="", stderr="", user_snippet="delete all",
        policy_blocked=True, policy_reason="strict_denylist"
    )
    result = await synthesize_lesson_text(llm, ws, inp, log_clip=500)
    assert "policy chặn" in result


@pytest.mark.asyncio
async def test_synthesize_lesson_llm_success():
    ws = _make_settings()
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": "Lệnh thành công, không rủi ro."}})
    inp = SandboxLessonInput(
        trace_id="trace-one", run_id="r1", command="kubectl get pods", exit_code=0,
        stdout="pod1 Running", stderr="", user_snippet="check pods",
        policy_blocked=False, policy_reason=""
    )
    result = await synthesize_lesson_text(llm, ws, inp, log_clip=500)
    assert "Lệnh thành công" in result


@pytest.mark.asyncio
async def test_synthesize_lesson_llm_error_fallback():
    ws = _make_settings()
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=Exception("llm down"))
    inp = SandboxLessonInput(
        trace_id="trace-one", run_id="r1", command="kubectl get pods", exit_code=1,
        stdout="", stderr="error", user_snippet="",
        policy_blocked=False, policy_reason=""
    )
    result = await synthesize_lesson_text(llm, ws, inp, log_clip=500)
    assert "lesson_error" in result


@pytest.mark.asyncio
async def test_synthesize_lesson_empty_content_fallback():
    ws = _make_settings()
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": ""}})
    inp = SandboxLessonInput(
        trace_id="trace-one", run_id="r1", command="kubectl get pods", exit_code=0,
        stdout="out", stderr="", user_snippet="",
        policy_blocked=False, policy_reason=""
    )
    result = await synthesize_lesson_text(llm, ws, inp, log_clip=500)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# upsert_action_experience tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_action_experience_normal():
    vs = MagicMock()
    vs.upsert = AsyncMock()
    vec = _fake_vec()
    pid = await upsert_action_experience(vs, lesson="test lesson", vector=vec, payload={"x": 1})
    assert isinstance(pid, str)
    vs.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_action_experience_short_vector_padded():
    vs = MagicMock()
    vs.upsert = AsyncMock()
    # Provide a short vector — should be padded to EMBED_DIM
    short_vec = [0.1] * 10
    pid = await upsert_action_experience(vs, lesson="lesson", vector=short_vec, payload={})
    vs.upsert.assert_called_once()
    call_args = vs.upsert.call_args
    upserted_vec = call_args[1]["points"][0].vector
    assert len(upserted_vec) == EMBED_DIM


@pytest.mark.asyncio
async def test_upsert_action_experience_with_explicit_point_id():
    vs = MagicMock()
    vs.upsert = AsyncMock()
    pid = await upsert_action_experience(
        vs, lesson="l", vector=_fake_vec(), payload={}, point_id="explicit-id"
    )
    assert pid == "explicit-id"


@pytest.mark.asyncio
async def test_upsert_action_experience_auto_point_id_from_payload():
    vs = MagicMock()
    vs.upsert = AsyncMock()
    payload = {"trace_id": "t1", "run_id": "r1"}
    pid = await upsert_action_experience(vs, lesson="lesson text", vector=_fake_vec(), payload=payload)
    assert isinstance(pid, str)
    assert len(pid) > 0


# ---------------------------------------------------------------------------
# record_routing_from_success tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_routing_from_success_disabled():
    ctx = _make_ctx(routing_enabled=False)
    ctx.settings.routing_experience_enabled = False
    # Should return early without calling llm.embed
    await record_routing_from_success(ctx, tool="kubectl", args={}, trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_from_success_action_experience_disabled():
    ctx = _make_ctx(action_enabled=False)
    ctx.settings.action_experience_enabled = False
    await record_routing_from_success(ctx, tool="kubectl", args={}, trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_from_success_echo_tool_skipped():
    ctx = _make_ctx()
    await record_routing_from_success(ctx, tool="echo", args={}, trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_from_success_reply_tool_skipped():
    ctx = _make_ctx()
    await record_routing_from_success(ctx, tool="reply", args={}, trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_from_success_short_user_text_skipped():
    ctx = _make_ctx()
    ctx.inbound_user_text = "hi"
    await record_routing_from_success(ctx, tool="kubectl", args={}, trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_from_success_normal_upserts():
    ctx = _make_ctx()
    await record_routing_from_success(
        ctx,
        tool="kubectl",
        args={"namespace": "prod"},
        trace_id="trace-001",
    )
    ctx.llm.embed.assert_called_once()
    ctx.vector_store.upsert.assert_called_once()
    ctx.semaphore.acquire.assert_called_once()
    ctx.semaphore.release.assert_called_once()


@pytest.mark.asyncio
async def test_record_routing_from_success_slot_held_no_semaphore():
    ctx = _make_ctx()
    ctx.llm_slot_held = True
    await record_routing_from_success(ctx, tool="kubectl", args={}, trace_id="trace-one")
    ctx.semaphore.acquire.assert_not_called()
    ctx.semaphore.release.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_from_success_embed_error_swallowed():
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=Exception("embed fail"))
    # Should not raise
    await record_routing_from_success(ctx, tool="kubectl", args={}, trace_id="trace-one")
    ctx.semaphore.release.assert_called_once()


# ---------------------------------------------------------------------------
# record_agent_playbook_from_trajectory tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_agent_playbook_disabled_by_routing():
    ctx = _make_ctx()
    ctx.settings.routing_experience_enabled = False
    await record_agent_playbook_from_trajectory(
        ctx, user_text="test query", trajectory=[], trace_id="trace-one"
    )
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_agent_playbook_short_text_skipped():
    ctx = _make_ctx()
    await record_agent_playbook_from_trajectory(
        ctx, user_text="hi", trajectory=[], trace_id="trace-one"
    )
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_agent_playbook_only_mark_resolved_steps_skipped():
    ctx = _make_ctx()
    traj = [{"tool": "omni_mark_resolved", "args": {}}]
    await record_agent_playbook_from_trajectory(
        ctx, user_text="pod crash in prod namespace", trajectory=traj, trace_id="trace-one"
    )
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_agent_playbook_normal_upserts():
    ctx = _make_ctx()
    traj = [
        {"tool": "kubectl", "args": {"ns": "prod", "pod": "web-xxxx"}},
        {"tool": "omni_mark_resolved", "args": {}},
    ]
    await record_agent_playbook_from_trajectory(
        ctx,
        user_text="pod restart in production namespace",
        trajectory=traj,
        trace_id="trace-one",
        resolution_summary="Pod was OOMKilled, scaled up memory",
    )
    ctx.llm.embed.assert_called_once()
    ctx.vector_store.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_record_agent_playbook_embed_error_swallowed():
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=RuntimeError("embed fail"))
    traj = [{"tool": "helm", "args": {}}]
    await record_agent_playbook_from_trajectory(
        ctx, user_text="upgrade service in prod", trajectory=traj, trace_id="trace-one"
    )
    ctx.semaphore.release.assert_called_once()


@pytest.mark.asyncio
async def test_record_agent_playbook_args_from_last_non_resolved_step():
    ctx = _make_ctx()
    traj = [
        {"tool": "kubectl", "args": {"ns": "prod"}},
        {"tool": "helm", "args": {"chart": "nginx"}},
    ]
    await record_agent_playbook_from_trajectory(
        ctx,
        user_text="upgrade helm chart in prod namespace",
        trajectory=traj,
        trace_id="trace-one",
    )
    ctx.vector_store.upsert.assert_called_once()
    call_pts = ctx.vector_store.upsert.call_args[1]["points"]
    payload = call_pts[0].payload
    assert payload["tool"] == "helm"


@pytest.mark.asyncio
async def test_record_agent_playbook_non_dict_steps_skipped():
    ctx = _make_ctx()
    traj = ["not-a-dict", None, {"tool": "kubectl", "args": {"ns": "default"}}]
    await record_agent_playbook_from_trajectory(
        ctx, user_text="check pod status in default namespace", trajectory=traj, trace_id="trace-one"
    )
    ctx.llm.embed.assert_called_once()


# ---------------------------------------------------------------------------
# record_routing_exhausted_no_data tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_routing_exhausted_disabled():
    ctx = _make_ctx(action_enabled=False)
    ctx.settings.action_experience_enabled = False
    await record_routing_exhausted_no_data(ctx, "long query text", trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_exhausted_short_text_skipped():
    ctx = _make_ctx()
    await record_routing_exhausted_no_data(ctx, "hi", trace_id="trace-one")
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_routing_exhausted_normal():
    ctx = _make_ctx()
    from workers.slow_path_trace import AttemptRecord

    attempts = [
        AttemptRecord(attempt=1, phase="tool_error", error_signature="e1", one_line="fail", tool="kubectl"),
        AttemptRecord(attempt=2, phase="tool_error", error_signature="e2", one_line="fail2", tool="helm"),
    ]
    await record_routing_exhausted_no_data(
        ctx,
        "pod crash in production namespace",
        trace_id="trace-one",
        detail="no resolution",
        attempt_trace=attempts,
        exit_reason="max_attempts",
    )
    ctx.llm.embed.assert_called_once()
    ctx.vector_store.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_record_routing_exhausted_embed_error_swallowed():
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=RuntimeError("embed down"))
    await record_routing_exhausted_no_data(ctx, "pod crash in prod", trace_id="trace-one")
    ctx.semaphore.release.assert_called_once()


@pytest.mark.asyncio
async def test_record_routing_exhausted_slot_held():
    ctx = _make_ctx()
    ctx.llm_slot_held = True
    await record_routing_exhausted_no_data(ctx, "pod restart in production", trace_id="trace-one")
    ctx.semaphore.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# record_sandbox_lesson tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_sandbox_lesson_disabled():
    ctx = _make_ctx(action_enabled=False)
    ctx.settings.action_experience_enabled = False
    inp = SandboxLessonInput(
        trace_id="trace-t", run_id="r", command="ls", exit_code=0,
        stdout="", stderr="", user_snippet="", policy_blocked=False, policy_reason=""
    )
    await record_sandbox_lesson(ctx, inp)
    ctx.llm.embed.assert_not_called()


@pytest.mark.asyncio
async def test_record_sandbox_lesson_normal():
    ctx = _make_ctx()
    inp = SandboxLessonInput(
        trace_id="trace-t", run_id="r", command="kubectl get pods", exit_code=0,
        stdout="pod1 Running", stderr="", user_snippet="check pods",
        policy_blocked=False, policy_reason=""
    )
    await record_sandbox_lesson(ctx, inp)
    ctx.llm.chat.assert_called_once()
    ctx.llm.embed.assert_called_once()
    ctx.vector_store.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_record_sandbox_lesson_policy_blocked():
    ctx = _make_ctx()
    inp = SandboxLessonInput(
        trace_id="trace-t", run_id="r", command="rm -rf /", exit_code=-2,
        stdout="", stderr="", user_snippet="",
        policy_blocked=True, policy_reason="strict_denylist"
    )
    await record_sandbox_lesson(ctx, inp)
    # policy_blocked skips llm.chat for lesson but still embeds
    ctx.vector_store.upsert.assert_called_once()
    call_pts = ctx.vector_store.upsert.call_args[1]["points"]
    payload = call_pts[0].payload
    assert payload["safety_flag"] == "policy_blocked"


@pytest.mark.asyncio
async def test_record_sandbox_lesson_embed_error_swallowed():
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=RuntimeError("embed fail"))
    inp = SandboxLessonInput(
        trace_id="trace-t", run_id="r", command="ls", exit_code=0,
        stdout="", stderr="", user_snippet="", policy_blocked=False, policy_reason=""
    )
    # Should not raise
    await record_sandbox_lesson(ctx, inp)
    ctx.semaphore.release.assert_called_once()


@pytest.mark.asyncio
async def test_record_sandbox_lesson_slot_held():
    ctx = _make_ctx()
    ctx.llm_slot_held = True
    inp = SandboxLessonInput(
        trace_id="trace-t", run_id="r", command="ls", exit_code=0,
        stdout="", stderr="", user_snippet="", policy_blocked=False, policy_reason=""
    )
    await record_sandbox_lesson(ctx, inp)
    ctx.semaphore.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_action_experience_context tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_action_experience_context_disabled():
    ctx = _make_ctx(action_enabled=False)
    ctx.settings.action_experience_enabled = False
    result = await fetch_action_experience_context(ctx, "pod crash in prod")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_action_experience_context_short_query():
    ctx = _make_ctx()
    result = await fetch_action_experience_context(ctx, "hi")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_action_experience_context_empty_results():
    ctx = _make_ctx()
    ctx.vector_store.query_points = AsyncMock(return_value=QueryResponse(points=[]))
    result = await fetch_action_experience_context(ctx, "pod crash in production namespace")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_action_experience_context_filters_exhausted_source():
    ctx = _make_ctx()
    pt = PointStruct(
        id="p1",
        payload={"routing_source": ROUTING_SOURCE_SLOW_PATH_EXHAUSTED, "lesson": "exhausted lesson"},
        score=0.9,
    )
    ctx.vector_store.query_points = AsyncMock(return_value=QueryResponse(points=[pt]))
    result = await fetch_action_experience_context(ctx, "pod crash in production namespace")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_action_experience_context_returns_matching_lessons():
    ctx = _make_ctx()
    pt = PointStruct(
        id="p2",
        payload={"routing_source": ROUTING_SOURCE_SLOW_PATH, "lesson": "kubectl restart worked"},
        score=0.85,
    )
    ctx.vector_store.query_points = AsyncMock(return_value=QueryResponse(points=[pt]))
    result = await fetch_action_experience_context(ctx, "pod crash in production namespace")
    assert "action_experience" in result
    assert "kubectl restart worked" in result


@pytest.mark.asyncio
async def test_fetch_action_experience_context_exception_returns_empty():
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=Exception("embed down"))
    result = await fetch_action_experience_context(ctx, "pod crash in production namespace")
    assert result == ""
    ctx.semaphore.release.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_action_experience_context_skips_empty_lesson():
    ctx = _make_ctx()
    pt = PointStruct(
        id="p3",
        payload={"routing_source": ROUTING_SOURCE_AGENT_SESSION, "lesson": ""},
        score=0.8,
    )
    ctx.vector_store.query_points = AsyncMock(return_value=QueryResponse(points=[pt]))
    result = await fetch_action_experience_context(ctx, "pod crash in production")
    assert result == ""
