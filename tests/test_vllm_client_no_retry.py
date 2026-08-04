"""#39 — VLLMClient KHÔNG được để openai SDK tự retry ẩn.

Ground truth 2026-08-04 (drill payment-api): 2 lệnh LLM lỗi đo được duration_ms
385258/361343 dù `timeout_s=120` — khớp chính xác ~2x/~3x timeout_s, vì
`openai.AsyncOpenAI` mặc định `max_retries=2` (3 lần thử tổng) HOÀN TOÀN ẩn với
caller. `services.analyst.diagnosis_loop` đã tự có circuit-breaker riêng
(`_MAX_CONSECUTIVE_LLM_ERRORS`) dựa trên giả định "mỗi lượt tốn tối đa
timeout_s" — retry ẩn của SDK phá giả định đó, và khi Ollama quá tải, mỗi "1
lượt gọi" logic lại âm thầm biến thành 3 request xếp hàng, tự khuếch đại đúng
cơn quá tải gây ra lỗi.
"""

from __future__ import annotations

from llm.vllm_client import VLLMClient


def test_chat_client_has_retries_disabled():
    client = VLLMClient(base_url="http://x/v1", embed_url="http://x/v1", timeout_s=120.0)
    assert client._chat_client.max_retries == 0, (
        "openai SDK mặc định max_retries=2 — mỗi lệnh chờ tới 3x timeout_s trước khi "
        "lỗi thật sự nổi lên, phá giả định circuit-breaker của diagnosis_loop"
    )


def test_embed_client_has_retries_disabled():
    client = VLLMClient(base_url="http://x/v1", embed_url="http://x/v1", timeout_s=120.0)
    assert client._embed_client.max_retries == 0
