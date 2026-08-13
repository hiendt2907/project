"""Coverage cho workers.tool_output_status — nguồn phân loại thành công/thất bại DUY
NHẤT dùng chung bởi proactive_observer._quick_verify_output và tool_registry's
is_error_output. Xem docstring module để biết bối cảnh (bug thật đã xảy ra).
"""

from __future__ import annotations

from workers.tool_output_status import classify_tool_output


def test_explicit_status_tag_wins():
    assert classify_tool_output("[STATUS] ok\nanything else") == "ok"
    assert classify_tool_output("[STATUS] fail\nanything else") == "fail"


def test_three_way_status_tag_promql_style():
    assert classify_tool_output("[status] business_hit\nsome numbers") == "ok"
    assert classify_tool_output("[status] empty_result") == "fail"
    assert classify_tool_output("[status] error") == "fail"


def test_data_token_ok_suffix():
    assert classify_tool_output("[DATA] rollout_restart_ok deployment=x ns=y\n[DIAGNOSIS] done") == "ok"
    assert classify_tool_output("[DATA] scale_ok deployment=x\n[DIAGNOSIS] done") == "ok"
    assert classify_tool_output("[DATA] kubectl_ok exit=0\n[DIAGNOSIS] out") == "ok"


def test_data_token_known_fail_tokens():
    assert classify_tool_output(
        "[DATA] deployment_not_found deployment='nginx-test' ns=multi-agent\n"
        "[DIAGNOSIS] Không có Deployment."
    ) == "fail"
    assert classify_tool_output("[DATA] ambiguous_deployment\n[DIAGNOSIS] nhiều deployment khớp") == "fail"
    assert classify_tool_output("[DATA] api_error\n[DIAGNOSIS] Kubernetes API (500): x") == "fail"
    assert classify_tool_output("[DATA] confirm_required\n[DIAGNOSIS] cần xác nhận Telegram") == "fail"
    assert classify_tool_output("[DATA] stale_state\n[DIAGNOSIS] generation changed") == "fail"
    assert classify_tool_output("[DATA] khong_co_quyen\n[DIAGNOSIS] 403") == "fail"
    assert classify_tool_output("[DATA] no_redis\n[DIAGNOSIS] không có ctx.redis") == "fail"


def test_kubectl_exit_code_special_cased():
    # kubectl_exit_0 KHÔNG xảy ra thật (code==0 luôn đi qua nhánh kubectl_ok riêng), nhưng
    # hàm vẫn phải xử lý đúng nếu gặp — tránh coi mọi "kubectl_exit_" là fail vô điều kiện.
    assert classify_tool_output("[DATA] kubectl_exit_0\n[DIAGNOSIS] ...") == "ok"
    assert classify_tool_output("[DATA] kubectl_exit_1\n[DIAGNOSIS] stderr=boom") == "fail"


def test_unknown_when_no_signal():
    assert classify_tool_output("[DATA] rbac_hardened role updated\n[DIAGNOSIS] ok") == "unknown"
    assert classify_tool_output("plain text with no markers") == "unknown"


def test_empty_text_is_fail():
    assert classify_tool_output("") == "fail"
    assert classify_tool_output("   ") == "fail"


def test_csv_fallback_only_applies_without_structural_signal():
    assert classify_tool_output("ok text but error_code_404", "error_code_404") == "fail"
    assert classify_tool_output("all looks good here", "bad_word") == "unknown"


def test_missing_arg_phrases_are_fail():
    assert classify_tool_output("thiếu args here") == "fail"
    assert classify_tool_output("missing arg: namespace") == "fail"
    assert classify_tool_output("invalid args passed") == "fail"


def test_never_raises_on_garbage_input():
    assert classify_tool_output(None) == "fail"  # type: ignore[arg-type]
