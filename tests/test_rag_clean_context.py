"""RAG clean_and_truncate_context — anti-GIGO before embed."""

from __future__ import annotations

from pkg.rag.gate import clean_and_truncate_context


def test_strips_k8s_api_error_noise() -> None:
    blob = """Status: Failure
Reason: BadRequest
Message: container foo not started
Details: very long line """ + "x" * 900
    out = clean_and_truncate_context(blob, {"alertname": "PodDown"}, max_tokens=512)
    assert "alert_name=PodDown" in out or "alertname=PodDown" in out
    assert len(out) < len(blob)


def test_truncates_to_reasonable_length() -> None:
    long_log = "\n".join([f"line {i} " + "z" * 200 for i in range(200)])
    out = clean_and_truncate_context(long_log, None, max_tokens=256)
    assert len(out) < len(long_log)
