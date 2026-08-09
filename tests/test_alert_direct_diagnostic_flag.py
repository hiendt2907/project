"""S5 — cờ `alert_direct_diagnostic_enabled` biến alert từ đường khởi phát thành nguồn thu.

Mục đích của bộ test này KHÔNG phải chứng minh cờ chạy được, mà chốt ba điều dễ trôi:

1. Mặc định phải là ``True`` — deploy cờ này là NO-OP. Nếu ai đó đổi default thành
   ``False`` mà chưa qua cửa sổ quan sát 24h, alert sẽ im lặng chuyển hướng và không
   một lỗi nào được ném (đúng lớp "trường rơi im lặng" đã trả giá ba lần ở Đ40).
2. Nhánh chuyển hướng phải publish ĐÚNG topic proactive, cùng shape ``model_dump()``
   như ``proactive_observer._push_incident`` — khác shape là consumer chết câm.
3. Nhánh chuyển hướng KHÔNG được emit ``TRANSITION_DIAGNOSED``. Chưa chẩn đoán gì mà
   ghi "đã chẩn đoán" là tự khen bằng số — chính cái bẫy GIGO đang gỡ.
"""

from __future__ import annotations

import inspect
import re

from workers.settings import WorkerSettings


def _stream_entry_source() -> str:
    """Nguồn của `_process_stream_entry`, đã bỏ comment.

    Bỏ comment là bắt buộc: một bộ test trước đây đã tự cho mình điểm đậu vì regex
    khớp trúng chính câu comment giải thích, không phải code.
    """
    from workers import omni_worker

    src = inspect.getsource(omni_worker._process_stream_entry)
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def test_default_is_true_so_deploying_the_flag_changes_nothing() -> None:
    assert WorkerSettings().alert_direct_diagnostic_enabled is True


def test_flag_reads_the_documented_env_name() -> None:
    field = WorkerSettings.model_fields["alert_direct_diagnostic_enabled"]
    aliases = getattr(field.validation_alias, "choices", [])
    assert "OMNI_ALERT_DIRECT_DIAGNOSTIC_ENABLED" in [str(a) for a in aliases]


def test_collector_branch_publishes_to_the_proactive_topic() -> None:
    src = _stream_entry_source()
    assert "alert_direct_diagnostic_enabled" in src
    assert "kafka_topic_proactive_incidents" in src
    assert "send_envelope_inner" in src


def test_collector_branch_does_not_claim_the_alert_was_diagnosed() -> None:
    """`TRANSITION_DIAGNOSED` chỉ được nằm trong nhánh chạy pipeline thật."""
    src = _stream_entry_source()
    guard = src.index("alert_direct_diagnostic_enabled")
    else_at = src.index("else:", guard)
    redirect_branch = src[guard:else_at]
    assert "TRANSITION_DIAGNOSED" not in redirect_branch
    assert "run_diagnostic_pipeline" not in redirect_branch
    assert "TRANSITION_DIAGNOSED" in src[else_at:]


def test_diagnostic_pipeline_still_runs_on_the_default_path() -> None:
    src = _stream_entry_source()
    assert re.search(r"else:\s*\n\s*await run_diagnostic_pipeline\(ctx, ev\)", src)
