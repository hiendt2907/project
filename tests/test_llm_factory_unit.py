"""Unit tests for LLM URL normalization, factory wiring, and env aliases."""

from __future__ import annotations

from llm.factory import build_llm_client
from llm.vllm_client import _base_url


def test_base_url_normalizes_v1_suffix():
    assert _base_url("http://ollama:11434") == "http://ollama:11434/v1"
    assert _base_url("http://ollama:11434/v1") == "http://ollama:11434/v1"
    assert _base_url("http://ollama:11434/v1/") == "http://ollama:11434/v1"


def test_build_llm_client_passes_timeout():
    client = build_llm_client(
        base_url="http://localhost:11434",
        embed_url="http://localhost:11434",
        timeout_s=77.0,
    )
    assert client.timeout_s == 77.0


def test_omni_ollama_base_url_alias(monkeypatch):
    monkeypatch.setenv("OMNI_OLLAMA_BASE_URL", "http://custom-host:11434/")
    monkeypatch.setenv("OMNI_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("OMNI_VLLM_BASE_URL", raising=False)

    from workers.settings import WorkerSettings

    ws = WorkerSettings()
    assert ws.vllm_base_url.startswith("http://custom-host:11434")
