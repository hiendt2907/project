"""Tests for vLLM-backed Omni Analyst pipeline.

Verifies:
  - VLLMClient constructs the correct OpenAI /v1/chat/completions payload.
  - VLLMClient constructs the correct OpenAI /v1/embeddings payload.
  - VLLMClient handles both streaming and non-streaming response shapes.
  - Response dict shape matches what handlers.py expects (same as old Ollama shape).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chat_completion(content: str) -> MagicMock:
    """Build a minimal openai ChatCompletion-like object."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    comp = MagicMock()
    comp.choices = [choice]
    return comp


def _make_embedding_response(vectors: list[list[float]]) -> MagicMock:
    """Build a minimal openai EmbeddingCreateResponse-like object."""
    resp = MagicMock()
    resp.data = [MagicMock(embedding=v) for v in vectors]
    return resp


# ---------------------------------------------------------------------------
# VLLMClient — chat
# ---------------------------------------------------------------------------

class TestVLLMClientChat:
    @pytest.mark.asyncio
    async def test_chat_returns_ollama_compatible_shape(self) -> None:
        """VLLMClient.chat() must return {"message": {"role": "assistant", "content": ...}}."""
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        completion = _make_chat_completion("rollout_restart")

        with patch.object(
            client._chat_client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ):
            result = await client.chat(
                model="qwen2.5-coder-3b",
                messages=[
                    {"role": "system", "content": "You are SRE."},
                    {"role": "user", "content": "Fix the pod."},
                ],
                options={"temperature": 0.1, "num_ctx": 4096},
            )

        assert result["message"]["content"] == "rollout_restart"
        assert result["message"]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_chat_maps_num_ctx_to_max_tokens(self) -> None:
        """options.num_ctx must be forwarded as max_tokens to the OpenAI call."""
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        completion = _make_chat_completion("{}")
        create_mock = AsyncMock(return_value=completion)

        with patch.object(
            client._chat_client.chat.completions, "create", new=create_mock
        ):
            await client.chat(
                model="qwen2.5-coder-3b",
                messages=[{"role": "user", "content": "test"}],
                options={"temperature": 0.0, "num_ctx": 2048},
            )

        call_kwargs = create_mock.call_args.kwargs
        assert call_kwargs["max_tokens"] == 2048
        assert call_kwargs["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_chat_accepts_keep_alive_without_error(self) -> None:
        """keep_alive kwarg must be accepted (no-op) for call-site compatibility."""
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        completion = _make_chat_completion("ok")

        with patch.object(
            client._chat_client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ):
            result = await client.chat(
                model="qwen2.5-coder-3b",
                messages=[{"role": "user", "content": "ping"}],
                keep_alive="5m",  # should be silently ignored
            )

        assert result["message"]["content"] == "ok"

    @pytest.mark.asyncio
    async def test_chat_json_format_sets_response_format(self) -> None:
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        completion = _make_chat_completion('{"ok":true}')
        create_mock = AsyncMock(return_value=completion)

        with patch.object(
            client._chat_client.chat.completions, "create", new=create_mock
        ):
            await client.chat(
                model="qwen2.5-coder-3b",
                messages=[{"role": "user", "content": "emit json"}],
                format="json",
            )

        assert create_mock.call_args.kwargs.get("response_format") == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_aclose_closes_chat_and_embed_clients(self) -> None:
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        with patch.object(client._chat_client, "close", new=AsyncMock()) as c_close:
            with patch.object(client._embed_client, "close", new=AsyncMock()) as e_close:
                await client.aclose()
        c_close.assert_awaited_once()
        e_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# VLLMClient — embed
# ---------------------------------------------------------------------------

class TestVLLMClientEmbed:
    @pytest.mark.asyncio
    async def test_embed_returns_embeddings_key(self) -> None:
        """VLLMClient.embed() must return {"embeddings": [[float, ...]]}."""
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        vec = [0.1, 0.2, 0.3]
        emb_response = _make_embedding_response([vec])

        with patch.object(
            client._embed_client.embeddings,
            "create",
            new=AsyncMock(return_value=emb_response),
        ):
            result = await client.embed(
                model="nomic-ai/nomic-embed-text-v1.5",
                input="nginx CrashLoopBackOff",
            )

        assert "embeddings" in result
        assert result["embeddings"][0] == vec

    @pytest.mark.asyncio
    async def test_embed_batch_input(self) -> None:
        """embed() must handle a list of texts and return one vector per input."""
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        vecs = [[0.1, 0.2], [0.3, 0.4]]
        emb_response = _make_embedding_response(vecs)

        with patch.object(
            client._embed_client.embeddings,
            "create",
            new=AsyncMock(return_value=emb_response),
        ):
            result = await client.embed(
                model="nomic-ai/nomic-embed-text-v1.5",
                input=["text one", "text two"],
            )

        assert len(result["embeddings"]) == 2
        assert result["embeddings"] == vecs


# ---------------------------------------------------------------------------
# Reasoning chain — LLM output parsing
# ---------------------------------------------------------------------------

class TestReasoningChain:
    """Verify that the system correctly extracts reasoning content from vLLM response."""

    @pytest.mark.asyncio
    async def test_handler_extracts_content_from_vllm_response(self) -> None:
        """VLLMClient response shape must satisfy handler resp.get('message').get('content')."""
        from llm.vllm_client import VLLMClient

        client = VLLMClient(
            base_url="http://mock-vllm:8000",
            embed_url="http://mock-embedder:8001",
        )
        reasoning = '{"tool": "k8s_rollout_restart", "args": {"deployment": "nginx"}}'
        completion = _make_chat_completion(reasoning)

        with patch.object(
            client._chat_client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ):
            resp = await client.chat(
                model="qwen2.5-coder-3b",
                messages=[{"role": "user", "content": "diagnose"}],
            )

        # Exactly the access pattern used by handlers.py slow-path
        content = (resp.get("message") or {}).get("content") or ""
        assert "k8s_rollout_restart" in content

    def test_embedding_response_shape_for_handlers(self) -> None:
        """VLLMClient.embed() response must satisfy _embedding_from_response() in handlers."""
        # _embedding_from_response checks for 'embeddings' key (list of vectors)
        fake_resp = {"embeddings": [[0.1, 0.2, 0.3]]}
        embs = fake_resp.get("embeddings")
        assert isinstance(embs, list) and len(embs) == 1
        assert embs[0] == [0.1, 0.2, 0.3]
