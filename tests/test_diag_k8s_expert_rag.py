"""Diagnostic path injects k8s_expert RAG when sanitized."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
from workers.reasoning_evidence_inbound import reason_diagnostic_evidence_only


@pytest.mark.asyncio
async def test_fetch_k8s_expert_context_returns_blocks() -> None:
    pt = MagicMock()
    pt.score = 0.77
    pt.payload = {
        "text": "Pods may be evicted when node pressure occurs.",
        "metadata": {"url": "https://kubernetes.io/docs/foo/", "version": "1.30"},
    }
    resp = MagicMock()
    resp.points = [pt]
    vs = MagicMock()
    vs.similarity_search = AsyncMock(return_value=resp)
    ws = MagicMock()
    ws.diag_k8s_expert_rag_enabled = True
    ws.diag_k8s_expert_rag_limit = 4
    ws.diag_k8s_expert_rag_score_threshold = 0.4
    ws.diag_k8s_expert_rag_max_chars = 8000
    ws.diag_k8s_expert_rag_query_max_chars = 4000
    ws.embed_model = "m"
    ws.ollama_keep_alive = "5m"
    ws.pgvector_collection_k8s_expert = "k8s_expert"
    ctx = MagicMock()
    ctx.settings = ws
    ctx.vector_store = vs
    ctx.ollama = MagicMock()
    out = await fetch_k8s_expert_context_for_diagnostic(ctx, "pod evicted pressure " * 2)
    assert "[CONTEXT: k8s_expert" in out
    assert "kubernetes.io" in out
    assert "evicted" in out.lower()
    vs.similarity_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_reason_diagnostic_sanitized_prepends_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _kb(*_a: object, **_k: object) -> str:
        return "[CONTEXT: k8s_expert score=0.900 url=https://x]\nref text"

    monkeypatch.setattr(
        "workers.reasoning_evidence_inbound.fetch_k8s_expert_context_for_diagnostic",
        _kb,
    )
    ws = MagicMock()
    ws.diag_k8s_expert_rag_enabled = True
    ws.model_reasoning_engine = "m"
    ws.ollama_keep_alive = "5m"
    ws.omni_summary_max_words = 100
    ctx = MagicMock()
    ctx.settings = ws
    ctx.scout_ready = MagicMock()
    ctx.scout_ready.is_set = MagicMock(return_value=True)
    captured: dict[str, str] = {}

    async def _chat(*_a: object, **kw: object) -> dict:
        msgs = kw.get("messages") or []
        if msgs:
            captured["user"] = str(msgs[-1].get("content") or "")
            captured["system"] = str(msgs[0].get("content") or "")
        return {"message": {"content": "OK"}}

    ctx.ollama = MagicMock()
    ctx.ollama.chat = AsyncMock(side_effect=_chat)
    out = await reason_diagnostic_evidence_only(
        ctx,
        {
            "text": "SDK block here " * 5,
            "diagnostic_evidence_sanitized": True,
        },
        "t1",
    )
    assert out == "OK"
    assert "[CONTEXT: k8s_expert" in captured.get("user", "")
    assert "[DIAGNOSTIC_EVIDENCE]" in captured.get("user", "")
    assert "CONTEXT_POLICY" in captured.get("system", "")


@pytest.mark.asyncio
async def test_reason_diagnostic_rag_gate_evaluated_skips_kb_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom_kb(*_a: object, **_k: object) -> str:
        raise AssertionError("fetch_k8s_expert_context_for_diagnostic must not run when rag_gate_evaluated")

    monkeypatch.setattr(
        "workers.reasoning_evidence_inbound.fetch_k8s_expert_context_for_diagnostic",
        boom_kb,
    )
    ws = MagicMock()
    ws.diag_k8s_expert_rag_enabled = True
    ws.model_reasoning_engine = "m"
    ws.ollama_keep_alive = "5m"
    ws.omni_summary_max_words = 100
    ctx = MagicMock()
    ctx.settings = ws
    ctx.scout_ready = MagicMock()
    ctx.scout_ready.is_set = MagicMock(return_value=True)
    captured: dict[str, str] = {}

    async def _chat(*_a: object, **kw: object) -> dict:
        msgs = kw.get("messages") or []
        if msgs:
            captured["user"] = str(msgs[-1].get("content") or "")
        return {"message": {"content": "OK no kb"}}

    ctx.ollama = MagicMock()
    ctx.ollama.chat = AsyncMock(side_effect=_chat)
    out = await reason_diagnostic_evidence_only(
        ctx,
        {
            "text": "SDK block here " * 5,
            "diagnostic_evidence_sanitized": True,
            "rag_gate_evaluated": True,
        },
        "t-rg",
    )
    assert out == "OK no kb"
    assert "[CONTEXT: k8s_expert" not in captured.get("user", "")
    assert "SDK block" in captured.get("user", "")
