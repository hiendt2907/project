"""finish_reason phải đi xuyên từ provider lên tầng handler.

Bối cảnh (Đ74, 2026-08-17): đổi provider sang NIM `llama-3.1-8b` làm 19/23 golden
case rớt về 0 điểm. Nguyên nhân là JSON advisory bị cắt ở `num_predict`, KHÔNG
phải model kém — provider đã báo `finish_reason="length"` trong mọi phản hồi
nhưng không chỗ nào trong repo đọc trường đó, nên tầng trên chỉ thấy "JSON hỏng".
Bộ test này khoá lại đường truyền đó.
"""
from __future__ import annotations

import logging

import pytest

from llm.vllm_client import TRUNCATED_FINISH_REASONS, _note_finish_reason


class TestTruncatedFinishReasons:
    def test_openai_compat_and_ollama_spellings_both_covered(self) -> None:
        # NIM/vLLM trả "length"; Ollama native trả done_reason="length".
        assert "length" in TRUNCATED_FINISH_REASONS
        # Một số backend OpenAI-compat dùng "max_tokens".
        assert "max_tokens" in TRUNCATED_FINISH_REASONS

    def test_stop_is_not_truncation(self) -> None:
        assert "stop" not in TRUNCATED_FINISH_REASONS


class TestNoteFinishReason:
    def _call(self, reason: str | None) -> list[logging.LogRecord]:
        import llm.vllm_client as mod

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        mod.logger.addHandler(handler)
        try:
            _note_finish_reason(
                reason,
                model="meta/llama-3.1-8b-instruct",
                kind_label="structured",
                format_json=True,
                content='{"verdict": "CRIT',
                max_tokens=512,
            )
        finally:
            mod.logger.removeHandler(handler)
        return records

    def test_warns_on_length(self) -> None:
        records = self._call("length")
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "llm_response_truncated" in records[0].getMessage()
        # Phải nêu được cách sửa, không chỉ báo lỗi.
        assert "num_predict" in records[0].getMessage()

    def test_case_insensitive(self) -> None:
        assert len(self._call("LENGTH")) == 1

    @pytest.mark.parametrize("reason", ["stop", "", None])
    def test_silent_on_normal_completion(self, reason: str | None) -> None:
        assert self._call(reason) == []


class TestAdvisoryHandlerDistinguishesTruncation:
    """Handler phải phân biệt 'bị cắt' với 'model nói sai' — hai nguyên nhân khác hẳn."""

    def test_truncated_reason_recognised_from_response_dict(self) -> None:
        # Chính biểu thức handler dùng để quyết định, giữ khớp với code thật.
        resp = {"message": {"content": '{"verdict": "CRIT'}, "finish_reason": "length"}
        assert str(resp.get("finish_reason") or "").lower() in TRUNCATED_FINISH_REASONS

    def test_genuine_parse_failure_not_misreported_as_truncation(self) -> None:
        resp = {"message": {"content": "I cannot help with that."}, "finish_reason": "stop"}
        assert str(resp.get("finish_reason") or "").lower() not in TRUNCATED_FINISH_REASONS

    def test_missing_finish_reason_defaults_to_parse_failure(self) -> None:
        # Provider cũ / đường code chưa gắn finish_reason: không được kết luận bừa
        # là truncation.
        resp: dict = {"message": {"content": "oops"}}
        assert str(resp.get("finish_reason") or "").lower() not in TRUNCATED_FINISH_REASONS
