"""Ollama client always sends num_ctx=4096 on chat and embed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.ollama_client import DEFAULT_NUM_CTX, OllamaClient


@pytest.mark.asyncio
async def test_chat_json_includes_num_ctx_4096() -> None:
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"done": True}
    mock_resp.raise_for_status = MagicMock()
    client._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

    await client.chat(
        model="qwen2.5:7b",
        messages=[{"role": "user", "content": "ping"}],
        keep_alive="5m",
    )

    kwargs = client._client.post.call_args.kwargs
    assert kwargs["json"]["options"]["num_ctx"] == DEFAULT_NUM_CTX == 4096
    assert kwargs["json"]["keep_alive"] == "5m"


@pytest.mark.asyncio
async def test_embed_json_includes_num_ctx_4096() -> None:
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"embeddings": [[]]}
    mock_resp.raise_for_status = MagicMock()
    client._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

    await client.embed(model="nomic-embed-text:latest", input="hello", keep_alive="5m")

    kwargs = client._client.post.call_args.kwargs
    assert kwargs["json"]["options"]["num_ctx"] == 4096
    assert kwargs["json"]["keep_alive"] == "5m"


@pytest.mark.asyncio
async def test_extra_options_cannot_override_num_ctx() -> None:
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"done": True}
    mock_resp.raise_for_status = MagicMock()
    client._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

    await client.chat(
        model="qwen2.5:7b",
        messages=[{"role": "user", "content": "x"}],
        options={"num_ctx": 8192, "temperature": 0.1},
    )
    assert client._client.post.call_args.kwargs["json"]["options"]["num_ctx"] == 4096
    assert client._client.post.call_args.kwargs["json"]["options"]["temperature"] == 0.1
