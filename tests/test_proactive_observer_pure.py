"""Pure helper coverage for workers.proactive_observer (W3)."""

from __future__ import annotations

from workers import proactive_observer as po
from workers.proactive_models import AnomalyEvent


def test_negative_pattern_redis_key():
    assert po._negative_pattern_redis_key("abc") == "omni:learning:negative:proactive:abc"


def test_sanitize_proactive_telegram_body():
    raw = "[DEBUG] secret\nvisible line\n[DETAIL] noise\nok"
    out = po._sanitize_proactive_telegram_body(raw)
    assert "secret" not in out
    assert "visible line" in out
    assert "ok" in out
    empty = po._sanitize_proactive_telegram_body("[DEBUG] x\n[DETAIL] y")
    assert "[OPERATOR_VIEW]" in empty


def test_react_mem_key():
    assert po._react_mem_key("tid-1") == "omni:proactive:react_mem:tid-1"


def test_stable_args_hash_order_independent():
    h1 = po._stable_args_hash({"b": 1, "a": 2})
    h2 = po._stable_args_hash({"a": 2, "b": 1})
    assert h1 == h2
    assert len(h1) == 24


def test_pattern_key_from_event():
    ev = AnomalyEvent(
        trace_id="trace-1234",
        rule_name="R1",
        canonical_query="up == 0",
        threshold=0.25,
    )
    k = po._pattern_key_from_event(ev)
    assert len(k) == 24
    k2 = po._pattern_key_from_event(ev)
    assert k == k2


def test_quick_verify_output():
    assert po._quick_verify_output("[status] business_hit\n", "") is True
    assert po._quick_verify_output("[status] empty_result", "") is False
    assert po._quick_verify_output("[status] error", "") is False
    assert po._quick_verify_output("thiếu args here", "") is False
    assert po._quick_verify_output("  ", "") is False
    assert po._quick_verify_output("ok text", "bad") is True
    assert po._quick_verify_output("ok but badword", "badword") is False


def test_result_status():
    assert po._result_status("[status] business_hit") == "business_hit"
    assert po._result_status("[STATUS] EMPTY_RESULT") == "empty_result"
    assert po._result_status("prefix [status] error tail") == "error"
    assert po._result_status("unknown") == "unknown"


def test_allow_learning_upsert():
    assert po._allow_learning_upsert("promql_instant", "[status] business_hit", True) is True
    assert po._allow_learning_upsert("vm_promql_instant", "[status] business_hit", True) is True
    assert po._allow_learning_upsert("promql_instant", "[status] error", True) is False
    assert po._allow_learning_upsert("promql_instant", "x", False) is False
    assert po._allow_learning_upsert("kubectl_get", "anything", True) is True


def test_embedding_from_response():
    assert po._embedding_from_response({"embedding": [0.1, 0.2]}) == [0.1, 0.2]
    assert po._embedding_from_response({"embedding": (0.5,)}) == [0.5]
    assert po._embedding_from_response({"embeddings": [[1.0, 2.0]]}) == [1.0, 2.0]
    assert po._embedding_from_response({}) == []
