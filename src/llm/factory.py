"""Construct the async LLM client used by Omni workers."""

from __future__ import annotations

import os

from llm.protocol import LlmClient
from llm.vllm_client import VLLMClient

# nvidia/nv-embedqa-e5-v5 requires input_type + truncate in the request body
# (NIM-specific extension to the OpenAI /v1/embeddings contract).
_NIM_EMBED_EXTRA_BODY = {"input_type": "passage", "truncate": "END"}


def build_llm_client(
    *,
    base_url: str,
    embed_url: str,
    timeout_s: float,
) -> LlmClient:
    """Return production ``VLLMClient``.

    Provider is env-driven (``OMNI_LLM_PROVIDER``): "ollama" (default, local,
    no auth) or "nim" (NVIDIA NIM — Bearer auth via OMNI_NIM_API_KEY, OpenAI-compat
    only, client-side rate limit via OMNI_NIM_RATE_LIMIT_RPM). All callers of this
    factory get the switch for free — no per-call-site changes needed.
    """
    provider = (os.environ.get("OMNI_LLM_PROVIDER") or "ollama").strip().lower()
    api_key = (os.environ.get("OMNI_NIM_API_KEY") or "").strip() or "ollama"
    rate_limit_rpm: int | None = None
    embed_extra_body: dict[str, str] | None = None
    if provider == "nim":
        rate_limit_rpm = int(os.environ.get("OMNI_NIM_RATE_LIMIT_RPM", "40"))
        embed_extra_body = dict(_NIM_EMBED_EXTRA_BODY)
    return VLLMClient(
        base_url=base_url,
        embed_url=embed_url,
        timeout_s=timeout_s,
        provider=provider,
        api_key=api_key,
        rate_limit_rpm=rate_limit_rpm,
        embed_extra_body=embed_extra_body,
    )
