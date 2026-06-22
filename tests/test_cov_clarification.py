"""Coverage tests for src/workers/clarification.py."""
from __future__ import annotations

import os

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

import json
from dataclasses import dataclass

import fakeredis.aioredis
import pytest


@dataclass
class _FakeLearnedContext:
    clarification_bypass: bool = False


# ---------------------------------------------------------------------------
# redis_key_wait_target
# ---------------------------------------------------------------------------

def test_redis_key_wait_target():
    from workers.clarification import redis_key_wait_target
    assert redis_key_wait_target(12345) == "wait_target:12345"


# ---------------------------------------------------------------------------
# parse_namespace_hint
# ---------------------------------------------------------------------------

def test_parse_namespace_hint_empty():
    from workers.clarification import parse_namespace_hint
    assert parse_namespace_hint("") is None


def test_parse_namespace_hint_namespace_prefix():
    from workers.clarification import parse_namespace_hint
    assert parse_namespace_hint("ở namespace multi-agent") == "multi-agent"


def test_parse_namespace_hint_ns_shorthand():
    from workers.clarification import parse_namespace_hint
    assert parse_namespace_hint("ns multi-agent") == "multi-agent"


def test_parse_namespace_hint_ns_colon():
    from workers.clarification import parse_namespace_hint
    assert parse_namespace_hint("ns=production") == "production"


def test_parse_namespace_hint_no_match():
    from workers.clarification import parse_namespace_hint
    assert parse_namespace_hint("kiểm tra cpu") is None


# ---------------------------------------------------------------------------
# _has_explicit_target (internal, tested via is_scope_ambiguous_cpu_ram)
# ---------------------------------------------------------------------------

def test_has_explicit_target_host_keyword():
    from workers.clarification import _has_explicit_target
    assert _has_explicit_target("check cpu on host machine") is True


def test_has_explicit_target_node_keyword():
    from workers.clarification import _has_explicit_target
    assert _has_explicit_target("worker node cpu usage") is True


def test_has_explicit_target_namespace_keyword():
    from workers.clarification import _has_explicit_target
    assert _has_explicit_target("check cpu in namespace default") is True


def test_has_explicit_target_kubectl_n_flag():
    from workers.clarification import _has_explicit_target
    assert _has_explicit_target("kubectl top pods -n multi-agent") is True


def test_has_explicit_target_k8s_hash_name():
    from workers.clarification import _has_explicit_target
    # Deployment pod name format: name-hash-hash
    assert _has_explicit_target("logs for omni-worker-abc12-xyz99") is True


def test_has_explicit_target_pod_keyword_with_name():
    from workers.clarification import _has_explicit_target
    assert _has_explicit_target("pod omni-worker") is True


def test_has_explicit_target_ambiguous():
    from workers.clarification import _has_explicit_target
    assert _has_explicit_target("kiểm tra cpu") is False


# ---------------------------------------------------------------------------
# is_scope_ambiguous_cpu_ram
# ---------------------------------------------------------------------------

def test_is_scope_ambiguous_true():
    from workers.clarification import is_scope_ambiguous_cpu_ram
    assert is_scope_ambiguous_cpu_ram("kiểm tra cpu") is True


def test_is_scope_ambiguous_too_short():
    from workers.clarification import is_scope_ambiguous_cpu_ram
    assert is_scope_ambiguous_cpu_ram("cpu") is False


def test_is_scope_ambiguous_context_present():
    from workers.clarification import is_scope_ambiguous_cpu_ram
    # "[CONTEXT:" + "mục tiêu" in text → bypass
    assert is_scope_ambiguous_cpu_ram("[CONTEXT: mục tiêu = HOST.]") is False


def test_is_scope_ambiguous_no_resource_keyword():
    from workers.clarification import is_scope_ambiguous_cpu_ram
    assert is_scope_ambiguous_cpu_ram("kiểm tra disk usage") is False


def test_is_scope_ambiguous_explicit_host():
    from workers.clarification import is_scope_ambiguous_cpu_ram
    assert is_scope_ambiguous_cpu_ram("check cpu on host") is False


def test_is_scope_ambiguous_ram():
    from workers.clarification import is_scope_ambiguous_cpu_ram
    assert is_scope_ambiguous_cpu_ram("kiểm tra ram toàn bộ") is True


# ---------------------------------------------------------------------------
# is_ambiguous_resource_check
# ---------------------------------------------------------------------------

def test_is_ambiguous_resource_check_basic():
    from workers.clarification import is_ambiguous_resource_check
    assert is_ambiguous_resource_check("kiểm tra cpu") is True


def test_is_ambiguous_resource_check_with_bypass_learned():
    from workers.clarification import is_ambiguous_resource_check
    learned = _FakeLearnedContext(clarification_bypass=True)
    assert is_ambiguous_resource_check("kiểm tra cpu", learned=learned) is False


def test_is_ambiguous_resource_check_with_state_host():
    from workers.clarification import is_ambiguous_resource_check
    from workers.session_state import SessionState
    state = SessionState(monitoring_target_type="host")
    assert is_ambiguous_resource_check("kiểm tra cpu", state=state) is False


def test_is_ambiguous_resource_check_with_context():
    from workers.clarification import is_ambiguous_resource_check
    assert is_ambiguous_resource_check("[CONTEXT: mục tiêu = HOST.]") is False


def test_is_ambiguous_resource_check_no_resource():
    from workers.clarification import is_ambiguous_resource_check
    assert is_ambiguous_resource_check("list all pods") is False


def test_is_ambiguous_resource_check_memory_vi():
    from workers.clarification import is_ambiguous_resource_check
    assert is_ambiguous_resource_check("kiểm tra bộ nhớ") is True


def test_is_ambiguous_resource_check_short_text():
    from workers.clarification import is_ambiguous_resource_check
    assert is_ambiguous_resource_check("ram") is False


# ---------------------------------------------------------------------------
# parse_resource_followup
# ---------------------------------------------------------------------------

def test_parse_resource_followup_empty():
    from workers.clarification import parse_resource_followup
    assert parse_resource_followup("") is None


def test_parse_resource_followup_host_en():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("host")
    assert result == ("host", None)


def test_parse_resource_followup_node():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("worker node")
    assert result is not None
    assert result[0] == "host"


def test_parse_resource_followup_pod_vi():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("pod cụ thể đi")
    assert result is not None
    assert result[0] == "pod"


def test_parse_resource_followup_pod_with_name():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("pod omni-worker")
    assert result is not None
    assert result[0] == "pod"
    assert result[1] == "omni-worker"


def test_parse_resource_followup_namespace():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("theo namespace multi-agent")
    assert result is not None
    assert result[0] == "namespace"


def test_parse_resource_followup_number_1():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("1")
    assert result == ("host", None)


def test_parse_resource_followup_number_2():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("2")
    assert result == ("pod", None)


def test_parse_resource_followup_number_3():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("3")
    assert result is not None
    assert result[0] == "namespace"


def test_parse_resource_followup_mot_pod():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("một pod")
    assert result is not None
    assert result[0] == "pod"


def test_parse_resource_followup_bad_token():
    from workers.clarification import parse_resource_followup
    # "pod cụ" — "cụ" is a bad token, should return ("pod", None) not ("pod", "cụ")
    result = parse_resource_followup("pod cụ thể")
    assert result is not None
    assert result[0] == "pod"
    # detail should not be "cụ"
    assert result[1] != "cụ"


def test_parse_resource_followup_1_pod_cu_the():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("1 pod cụ thể")
    assert result is not None
    assert result[0] == "pod"


def test_parse_resource_followup_ns_flag():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("ns=production")
    assert result is not None
    assert result[0] == "namespace"


def test_parse_resource_followup_localhost():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("localhost")
    assert result is not None
    assert result[0] == "host"


def test_parse_resource_followup_unknown_text():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("something random without keywords")
    assert result is None


def test_parse_resource_followup_mot_vi():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("một")
    assert result == ("host", None)


def test_parse_resource_followup_hai_vi():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("hai")
    assert result == ("pod", None)


def test_parse_resource_followup_ba_vi():
    from workers.clarification import parse_resource_followup
    result = parse_resource_followup("ba")
    assert result is not None
    assert result[0] == "namespace"


# ---------------------------------------------------------------------------
# merge_clarification_context
# ---------------------------------------------------------------------------

def test_merge_clarification_context_host():
    from workers.clarification import merge_clarification_context
    result = merge_clarification_context(
        original_user_text="check cpu",
        followup_text="host",
        target="host",
        detail=None,
    )
    assert "HOST" in result
    assert "system_psutil" in result
    assert "check cpu" in result


def test_merge_clarification_context_pod_with_ns():
    from workers.clarification import merge_clarification_context
    result = merge_clarification_context(
        original_user_text="check cpu",
        followup_text="pod omni-worker",
        target="pod",
        detail="omni-worker",
        namespace_hint="multi-agent",
    )
    assert "POD" in result
    assert "multi-agent" in result
    assert "inspect_pod_deep" in result


def test_merge_clarification_context_pod_no_ns():
    from workers.clarification import merge_clarification_context
    result = merge_clarification_context(
        original_user_text="check cpu",
        followup_text="pod",
        target="pod",
        detail=None,
    )
    assert "resolve_pod_identity" in result


def test_merge_clarification_context_namespace():
    from workers.clarification import merge_clarification_context
    result = merge_clarification_context(
        original_user_text="check cpu",
        followup_text="namespace multi-agent",
        target="namespace",
        detail=None,
    )
    assert "list_namespace_pods" in result


def test_merge_clarification_context_with_detail():
    from workers.clarification import merge_clarification_context
    result = merge_clarification_context(
        original_user_text="check cpu",
        followup_text="pod omni-worker",
        target="pod",
        detail="omni-worker-abc",
    )
    assert "detail=omni-worker-abc" in result


# ---------------------------------------------------------------------------
# get_wait_payload / set_wait_monitoring / clear_wait (async Redis)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wait_payload_none_when_empty():
    from workers.clarification import get_wait_payload
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    result = await get_wait_payload(r, 111)
    assert result is None


@pytest.mark.asyncio
async def test_set_and_get_wait_monitoring():
    from workers.clarification import set_wait_monitoring, get_wait_payload
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await set_wait_monitoring(r, 222, "kiểm tra cpu toàn bộ", ttl_sec=60)
    result = await get_wait_payload(r, 222)
    assert result is not None
    assert result["state"] == "monitoring"
    assert result["original_text"] == "kiểm tra cpu toàn bộ"


@pytest.mark.asyncio
async def test_clear_wait():
    from workers.clarification import set_wait_monitoring, get_wait_payload, clear_wait
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await set_wait_monitoring(r, 333, "check ram", ttl_sec=60)
    await clear_wait(r, 333)
    result = await get_wait_payload(r, 333)
    assert result is None


@pytest.mark.asyncio
async def test_get_wait_payload_raw_monitoring_string():
    """Legacy format: raw value is 'monitoring' string (no JSON)."""
    from workers.clarification import get_wait_payload, redis_key_wait_target, WAIT_STATE_MONITORING
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.set(redis_key_wait_target(444), WAIT_STATE_MONITORING)
    result = await get_wait_payload(r, 444)
    assert result is not None
    assert result["state"] == WAIT_STATE_MONITORING
    assert result["original_text"] == ""


@pytest.mark.asyncio
async def test_get_wait_payload_invalid_json_returns_none():
    from workers.clarification import get_wait_payload, redis_key_wait_target
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.set(redis_key_wait_target(555), "not-valid-json")
    result = await get_wait_payload(r, 555)
    assert result is None


@pytest.mark.asyncio
async def test_get_wait_payload_wrong_state_json():
    """JSON with different state value → returns None."""
    from workers.clarification import get_wait_payload, redis_key_wait_target
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    payload = json.dumps({"state": "other_state", "original_text": "x"})
    await r.set(redis_key_wait_target(666), payload)
    result = await get_wait_payload(r, 666)
    assert result is None
