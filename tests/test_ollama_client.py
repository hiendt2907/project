"""Ollama client always sends num_ctx=4096 on chat and embed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.ollama_client import DEFAULT_NUM_CTX, OllamaClient


@pytest.mark.asyncio
async def test_chat_json_includes_num_ctx_4096() -> None:
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
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
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"embeddings": [[]]}
    mock_resp.raise_for_status = MagicMock()
    client._client.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

    await client.embed(model="nomic-embed-text:latest", input="hello", keep_alive="5m")

    kwargs = client._client.post.call_args.kwargs
    assert kwargs["json"]["options"]["num_ctx"] == 4096
    assert kwargs["json"]["keep_alive"] == "5m"


@pytest.mark.asyncio
async def test_embed_fallbacks_to_embeddings_legacy_on_404() -> None:
    """Older Ollama: /api/embed missing → 404; retry /api/embeddings + prompt."""
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_404 = MagicMock()
    mock_404.status_code = 404
    mock_ok = MagicMock()
    mock_ok.status_code = 200
    mock_ok.json.return_value = {"embedding": [0.1, 0.2]}
    mock_ok.raise_for_status = MagicMock()
    client._client.post = AsyncMock(side_effect=[mock_404, mock_ok])  # type: ignore[method-assign]

    out = await client.embed(model="nomic-embed-text:latest", input="hello", keep_alive="5m")

    assert client._client.post.call_count == 2
    second = client._client.post.call_args_list[1]
    assert second[0][0] == "/api/embeddings"
    assert second[1]["json"]["prompt"] == "hello"
    assert second[1]["json"]["model"] == "nomic-embed-text:latest"
    assert out["embedding"] == [0.1, 0.2]


@pytest.mark.asyncio
async def test_chat_fallbacks_to_generate_on_404() -> None:
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_404 = MagicMock()
    mock_404.status_code = 404
    mock_ok = MagicMock()
    mock_ok.status_code = 200
    mock_ok.json.return_value = {"response": "ok-from-generate", "done": True}
    mock_ok.raise_for_status = MagicMock()
    client._client.post = AsyncMock(side_effect=[mock_404, mock_ok])  # type: ignore[method-assign]

    out = await client.chat(
        model="qwen2.5:1.5b",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        keep_alive="5m",
    )

    assert client._client.post.call_count == 2
    gen = client._client.post.call_args_list[1][1]["json"]
    assert gen["model"] == "qwen2.5:1.5b"
    assert gen["system"] == "sys"
    assert gen["prompt"] == "hi"
    assert gen["options"]["num_ctx"] == 4096
    assert out["message"]["content"] == "ok-from-generate"


@pytest.mark.asyncio
async def test_extra_options_cannot_override_num_ctx() -> None:
    client = OllamaClient(base_url="http://ollama.test:11434")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
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
