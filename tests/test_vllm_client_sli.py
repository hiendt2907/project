"""VLLMClient streaming path records TTFT/TPS boundary metrics (mocked OpenAI transport)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_chat_streaming_invokes_observe_llm_client_sli(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import vllm_client as vc

    monkeypatch.setattr(vc, "_STREAM_FOR_SLI", True)
    monkeypatch.setattr(vc, "_SLI_METRICS", True)

    captured: dict[str, object] = {}

    def fake_observe(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("workers.metrics_exporter.observe_llm_client_sli", fake_observe)

    class _U:
        def __init__(self, n: int) -> None:
            self.completion_tokens = n

    class _Chunk:
        def __init__(self, piece: str | None = None, usage: object | None = None) -> None:
            self.choices = [
                SimpleNamespace(delta=SimpleNamespace(content=piece)),
            ]
            self.usage = usage

    async def agen():
        yield _Chunk("Hel")
        yield _Chunk("lo")
        yield _Chunk(None, _U(40))

    mock_chat = MagicMock()
    mock_chat.completions.create = AsyncMock(side_effect=lambda **kw: agen())
    mock_client = SimpleNamespace(chat=mock_chat)

    client = vc.VLLMClient(base_url="http://ollama.test/v1", embed_url="http://ollama.test/v1", timeout_s=30.0)
    object.__setattr__(client, "_chat_client", mock_client)

    out = await client.chat_plain(model="qwen3.6", messages=[{"role": "user", "content": "x"}])
    assert out["message"]["content"] == "Hello"
    mock_chat.completions.create.assert_called_once()
    call_kw = mock_chat.completions.create.call_args.kwargs
    assert call_kw.get("stream") is True
    assert captured.get("model") == "qwen3.6"
    assert captured.get("call_kind") == "chat"
    assert float(captured["ttft_seconds"]) >= 0.0
    assert float(captured["completion_seconds"]) >= float(captured["ttft_seconds"])
    assert int(captured["output_tokens"]) == 40


@pytest.mark.asyncio
async def test_chat_non_stream_records_wall_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import vllm_client as vc

    monkeypatch.setattr(vc, "_STREAM_FOR_SLI", False)
    monkeypatch.setattr(vc, "_SLI_METRICS", True)

    captured: dict[str, object] = {}

    def fake_observe(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("workers.metrics_exporter.observe_llm_client_sli", fake_observe)

    class _Msg:
        content = "ok"

    class _Choice:
        message = _Msg()

    class _Comp:
        choices = [_Choice()]
        usage = SimpleNamespace(completion_tokens=3)

    mock_chat = MagicMock()
    mock_chat.completions.create = AsyncMock(return_value=_Comp())
    mock_client = SimpleNamespace(chat=mock_chat)

    client = vc.VLLMClient(base_url="http://ollama.test/v1", embed_url="http://ollama.test/v1", timeout_s=30.0)
    object.__setattr__(client, "_chat_client", mock_client)

    out = await client.chat_plain(model="qwen3.6", messages=[{"role": "user", "content": "y"}])
    assert out["message"]["content"] == "ok"
    assert captured.get("output_tokens") == 3
