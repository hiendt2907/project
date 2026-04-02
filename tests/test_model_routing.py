"""Model routing dispatch + classify."""

from __future__ import annotations

from workers.model_routing import classify_route, dispatch_task


def test_classify_ops_prefers_heavy() -> None:
    assert classify_route("kiểm tra redis") == "heavy"
    assert classify_route("check pgvector health") == "heavy"
    assert classify_route("kubectl get pods") == "heavy"


def test_classify_heavy() -> None:
    assert classify_route("dự báo tải 24h") == "heavy"
    assert classify_route("lên phương án kiến trúc") == "heavy"


def test_classify_reasoning() -> None:
    assert classify_route("tại sao pod crash") == "reasoning"
    assert classify_route("phân tích lỗi này") == "reasoning"


def test_dispatch_json_fail_escalation() -> None:
    m = dispatch_task(
        model_default="a",
        model_reasoning="b",
        model_heavy="c",
        user_text="dự báo x",
        attempt=0,
        json_parse_failures=2,
    )
    assert m == "c"


def test_dispatch_heavy_when_no_escalation() -> None:
    m = dispatch_task(
        model_default="a",
        model_reasoning="b",
        model_heavy="c",
        user_text="forecast architecture plan",
        attempt=0,
        json_parse_failures=0,
    )
    assert m == "c"


def test_dispatch_ops_uses_heavy() -> None:
    m = dispatch_task(
        model_default="a",
        model_reasoning="b",
        model_heavy="c",
        user_text="kiểm tra redis pod",
        attempt=0,
        json_parse_failures=0,
    )
    assert m == "c"


def test_dispatch_reasoning_uses_reasoning_engine() -> None:
    m = dispatch_task(
        model_default="a",
        model_reasoning="b",
        model_heavy="c",
        user_text="tại sao pod lại crash",
        attempt=0,
        json_parse_failures=0,
    )
    assert m == "b"
