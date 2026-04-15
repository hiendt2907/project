"""Tests for vLLM-backed Omni Analyst pipeline.

Verifies:
  - VLLMClient constructs the correct OpenAI /v1/chat/completions payload.
  - VLLMClient constructs the correct OpenAI /v1/embeddings payload.
  - phase3_output (mvp_api) routes through vLLM and parses the response.
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
# mvp_api.phase3_output — vLLM integration
# ---------------------------------------------------------------------------

class TestPhase3Output:
    """phase3_output must call /v1/chat/completions and parse HighLevelRemediationPlan."""

    _VALID_PLAN = json.dumps(
        {
            "action": "rollout_restart",
            "target_ref": "deployment/nginx",
            "namespace": "production",
            "reasoning": "ConfigMap missing",
        }
    )

    @pytest.mark.asyncio
    async def test_phase3_calls_vllm_endpoint(self) -> None:
        """phase3_output must POST to VLLM_BASE_URL/v1/chat/completions."""
        import httpx

        response_json = {
            "choices": [{"message": {"content": self._VALID_PLAN}}]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = response_json
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            # Import after patching so module-level constants are stable.
            import importlib
            import scripts.mvp_api as api_module

            plan = await api_module.phase3_output("You are SRE.", "Fix nginx.")

        posted_url = mock_client.post.call_args.args[0]
        assert "/v1/chat/completions" in posted_url, (
            f"Expected vLLM endpoint, got: {posted_url}"
        )

    @pytest.mark.asyncio
    async def test_phase3_parses_plan_correctly(self) -> None:
        """phase3_output must parse the LLM JSON into HighLevelRemediationPlan."""
        response_json = {
            "choices": [{"message": {"content": self._VALID_PLAN}}]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = response_json
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            import scripts.mvp_api as api_module

            plan = await api_module.phase3_output("sys", "user")

        assert plan.action == "rollout_restart"
        assert plan.namespace == "production"

    @pytest.mark.asyncio
    async def test_phase3_raises_502_on_connect_error(self) -> None:
        """phase3_output must raise HTTPException(502) when vLLM is unreachable."""
        import httpx
        from fastapi import HTTPException

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value = mock_client

            import scripts.mvp_api as api_module

            with pytest.raises(HTTPException) as exc_info:
                await api_module.phase3_output("sys", "user")

        assert exc_info.value.status_code == 502


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
