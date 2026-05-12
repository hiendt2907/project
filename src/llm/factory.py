"""Construct the async LLM client used by Omni workers."""

from __future__ import annotations

from llm.protocol import LlmClient
from llm.vllm_client import VLLMClient


def build_llm_client(
    *,
    base_url: str,
    embed_url: str,
    timeout_s: float,
) -> LlmClient:
    """Return production ``VLLMClient`` with HTTP timeout aligned to worker settings."""
    return VLLMClient(base_url=base_url, embed_url=embed_url, timeout_s=timeout_s)
