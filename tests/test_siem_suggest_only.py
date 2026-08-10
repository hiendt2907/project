"""SIEM batches now flow through the same planner/tier×risk path as every other domain.

Đ49 B3/S0.3 (plans/finguard-to-smart-siem-merge-2026-08-04.md) removed the unconditional
"SIEM always suggest-only, never EXECUTE_MUTATE/HITL" short-circuit in
`evidence_consumer._emit_agentic_mutate_if_any` — that gate existed because FinGuard was an
uncontrolled external source. SIEM is now internal (Smart SIEM correlation engine) and is
gated by the normal tier×risk matrix + HITL-for-HIGH-risk like the other 8 domains, not by a
blanket SIEM-only bypass. `omni_siem_suggest_only` no longer has any effect on this path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest


def _siem_batch(incident_id: str = "inc-001", hitl_required: bool = True) -> list[dict]:
    labels = {
        "alertname": "SIEMKubernetesThreat",
        "siem_source": "omni_siem",
        "siem_incident_id": incident_id,
        "siem_category": "k8s_threat",
        "severity": "critical",
        "siem_hitl_required": "true" if hitl_required else "false",
        "namespace": "default",
        "deployment": "compromised-svc",
    }
    snippet = json.dumps({"labels": labels})
    return [
        {
            "probe": "siem_evidence",
            "trace_id": f"omni-siem-{incident_id}",
            "canonical_query_snippet": snippet,
            "alert_hint": "Privileged container breakout detected.",
            "extracted_fact": {"siem_source": "omni_siem"},
        }
    ]


def _non_siem_batch() -> list[dict]:
    return [
        {
            "probe": "k8s_clinical_pod_status",
            "trace_id": "local-trace-001",
            "canonical_query_snippet": json.dumps(
                {"labels": {"alertname": "KubePodCrashLoopBacking", "namespace": "default", "deployment": "api"}}
            ),
            "alert_hint": "Pod crash loop.",
            "extracted_fact": {"phase": "CrashLoopBackOff"},
        }
    ]


def _make_settings(**overrides):
    defaults = {
        "omni_siem_suggest_only": True,
        "trace_correlation_ping_enabled": True,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_audit_chain": "omni-audit-chain",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "omni_llm_first_autonomy_enabled": False,
        "omni_unrestricted_tool_execution": True,
        "omni_legacy_deterministic_fallback": False,
        "omni_planner_precondition_gate_enabled": False,
        "telegram_admin_chat_id": 9999,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _KafkaCapture:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))


@pytest.mark.asyncio
async def test_siem_batch_reaches_planner_regardless_of_suggest_only_flag(monkeypatch):
    """SIEM batch must reach the normal planner — omni_siem_suggest_only=True no longer
    short-circuits it (that gate only ever existed for the retired external FinGuard path)."""
    from workers import evidence_consumer as ec

    planner_called = []

    async def fake_planner(ctx, *, trace, sanitized_text, batch, **kw):
        planner_called.append(True)
        return None

    async def fake_blind(ctx, batch, *, sanitized_text, rag_match_text):
        return None

    monkeypatch.setattr(ec, "run_agentic_mutate_plan", fake_planner)
    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", fake_blind)

    kafka = _KafkaCapture()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = _make_settings(
        omni_siem_suggest_only=True,
        omni_unrestricted_tool_execution=False,
        omni_legacy_deterministic_fallback=False,
        telegram_admin_chat_id=9999,
    )
    ctx = SimpleNamespace(
        kafka=kafka,
        redis=redis,
        settings=settings,
        telegram=AsyncMock(),
        vector_store=None,
    )

    batch = _siem_batch(hitl_required=False)
    result = await ec._emit_agentic_mutate_if_any(
        ctx,
        "omni-siem-inc-002",
        batch,
        sanitized_text="Privileged container escape.",
    )

    assert planner_called, "SIEM batch must reach the planner — no more blanket suggest-only bypass"
    assert result is False  # planner returned None → no plan


@pytest.mark.asyncio
async def test_non_siem_batch_also_reaches_planner(monkeypatch):
    """Non-SIEM batches were never affected by the SIEM gate — still reach the planner."""
    from workers import evidence_consumer as ec

    planner_called = []

    async def fake_planner(ctx, *, trace, sanitized_text, batch, **kw):
        planner_called.append(True)
        return None

    async def fake_blind(ctx, batch, *, sanitized_text, rag_match_text):
        return None

    monkeypatch.setattr(ec, "run_agentic_mutate_plan", fake_planner)
    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", fake_blind)

    kafka = _KafkaCapture()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = _make_settings(
        omni_siem_suggest_only=True,
        omni_unrestricted_tool_execution=False,
        omni_legacy_deterministic_fallback=False,
    )
    ctx = SimpleNamespace(
        kafka=kafka,
        redis=redis,
        settings=settings,
        telegram=AsyncMock(),
        vector_store=None,
    )

    batch = _non_siem_batch()
    await ec._emit_agentic_mutate_if_any(
        ctx,
        "local-trace-001",
        batch,
        sanitized_text="Pod crash loop.",
    )

    assert planner_called, "Non-SIEM batch must reach the planner"
