"""slow_path_trace: signatures, streak, recovery text, autopsy."""

from __future__ import annotations

from workers.slow_path_trace import (
    AttemptRecord,
    build_slow_path_recovery_user_message,
    consecutive_same_signature_streak,
    format_slow_path_autopsy,
    primary_bucket_for_metrics,
    slow_path_error_signature,
)


def test_slow_path_error_signature_buckets() -> None:
    assert slow_path_error_signature("parse", "x") == "parse_json"
    assert slow_path_error_signature("empty_model", "") == "empty_model"
    assert slow_path_error_signature("unknown_tool", "", tool="kubectl") == "unknown_tool:kubectl"
    assert slow_path_error_signature("tool_error", "HTTP 403 Forbidden") == "tool_error:permission"
    assert slow_path_error_signature("tool_error", "Thiếu pod_name — x") == "tool_error:missing_pod"


def test_consecutive_same_signature_streak() -> None:
    t = [
        AttemptRecord(1, "parse", "parse_json", "a", "a"),
        AttemptRecord(2, "parse", "parse_json", "b", "b"),
        AttemptRecord(3, "parse", "parse_json", "c", "c"),
    ]
    assert consecutive_same_signature_streak(t) == 3
    t2 = [
        AttemptRecord(1, "parse", "parse_json", "a", "a"),
        AttemptRecord(2, "tool_error", "tool_error:other", "b", "b"),
    ]
    assert consecutive_same_signature_streak(t2) == 1


def test_primary_bucket_mixed() -> None:
    t = [
        AttemptRecord(1, "parse", "parse_json", "a", "a"),
        AttemptRecord(2, "parse", "tool_error:other", "b", "b"),
    ]
    assert primary_bucket_for_metrics(t) == "mixed"


def test_build_recovery_bounded() -> None:
    long_err = "E" * 2000
    tr = [
        AttemptRecord(1, "parse", "parse_json", "short", "d1"),
        AttemptRecord(2, "tool_error", "tool_error:other", "short2", long_err, tool="x"),
    ]
    msg = build_slow_path_recovery_user_message("user wants cpu check", tr)
    assert "[USER_GOAL]" in msg
    assert len(msg) < 3500
    assert "..." in msg or len(long_err) > 720


def test_format_autopsy_has_recommend() -> None:
    tr = [
        AttemptRecord(
            1,
            "tool_error",
            "tool_error:permission",
            "403",
            "403",
            tool="vm_promql_instant",
        ),
    ]
    out = format_slow_path_autopsy(max_attempts=5, attempt_trace=tr, exit_reason="max_attempts")
    assert "[DATA] autopsy_exhausted" in out
    assert "[RECOMMEND]" in out
    assert "vm_promql_instant" in out
    assert "RBAC" in out or "ServiceAccount" in out
