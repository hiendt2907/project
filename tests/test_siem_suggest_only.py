"""SIEM suggest-only mode: FinGuard batches must not emit EXECUTE_MUTATE or HITL_PENDING."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest


def _siem_batch(incident_id: str = "inc-001", hitl_required: bool = True) -> list[dict]:
    labels = {
        "alertname": "SIEMKubernetesThreat",
        "siem_source": "finguard",
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
            "trace_id": f"fg-{incident_id}",
            "canonical_query_snippet": snippet,
            "alert_hint": "Privileged container breakout detected.",
            "extracted_fact": {"siem_source": "finguard"},
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
async def test_siem_suggest_only_blocks_execute_mutate_and_hitl():
    """SIEM batch + omni_siem_suggest_only=True → only SUGGEST_REMEDIATION(SIEM_SUGGEST_ONLY), nothing on omni-actions mutate path."""
    from workers.evidence_consumer import _emit_agentic_mutate_if_any

    kafka = _KafkaCapture()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tg = AsyncMock()
    settings = _make_settings(omni_siem_suggest_only=True, telegram_admin_chat_id=9999)
    ctx = SimpleNamespace(
        kafka=kafka,
        redis=redis,
        settings=settings,
        telegram=tg,
        vector_store=None,
        inbound_trace_id=None,
    )

    batch = _siem_batch()
    result = await _emit_agentic_mutate_if_any(
        ctx,
        "fg-inc-001",
        batch,
        sanitized_text="Privileged container escape in default/compromised-svc.",
    )

    assert result is True, "SIEM suggest-only must return True (handled; caller must NOT fall through to RAG_MISS)"

    topics_used = {t for t, _ in kafka.sent}
    assert "omni-hitl-pending" not in topics_used, "emit_hitl_pending must NOT fire in suggest-only mode"

    # SUGGEST_REMEDIATION goes to omni-actions (action routing topic), not an execution
    for topic, payload in kafka.sent:
        if topic == "omni-actions":
            envelope = json.loads(payload["data"])
            assert envelope.get("action") == "SUGGEST_REMEDIATION", (
                f"Only SUGGEST_REMEDIATION allowed on omni-actions for SIEM; got {envelope.get('action')!r}"
            )
            # source is inside the nested data dict
            inner = envelope.get("data", {})
            assert inner.get("source") == "SIEM_SUGGEST_ONLY"

    # Telegram must be sent to admin chat
    tg.send_message.assert_called_once()
    call_args = tg.send_message.call_args
    assert call_args[0][0] == 9999
    msg = call_args[0][1]
    assert "cần người phê duyệt" in msg
    assert "inc-001" in msg


@pytest.mark.asyncio
async def test_siem_suggest_only_disabled_does_not_short_circuit(monkeypatch):
    """omni_siem_suggest_only=False → falls through to normal planner path (no short-circuit)."""
    from workers import evidence_consumer as ec

    planner_called = []

    async def fake_planner(ctx, *, trace, sanitized_text, batch, **kw):
        planner_called.append(True)
        return None  # no plan → returns False from outer function

    async def fake_blind(ctx, batch, *, sanitized_text, rag_match_text):
        return None

    monkeypatch.setattr(ec, "run_agentic_mutate_plan", fake_planner)
    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", fake_blind)

    kafka = _KafkaCapture()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = _make_settings(
        omni_siem_suggest_only=False,
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
        "fg-inc-002",
        batch,
        sanitized_text="Privileged container escape.",
    )

    assert planner_called, "Planner must be invoked when suggest-only is disabled"
    assert result is False  # planner returned None → no plan


@pytest.mark.asyncio
async def test_non_siem_batch_not_intercepted(monkeypatch):
    """Non-SIEM batches must never be caught by the SIEM short-circuit."""
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


@pytest.mark.asyncio
async def test_siem_suggest_only_no_telegram_without_admin_cid():
    """No Telegram sent when telegram_admin_chat_id is None — just log a warning."""
    from workers.evidence_consumer import _emit_agentic_mutate_if_any

    kafka = _KafkaCapture()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tg = AsyncMock()
    settings = _make_settings(omni_siem_suggest_only=True, telegram_admin_chat_id=None)
    ctx = SimpleNamespace(
        kafka=kafka,
        redis=redis,
        settings=settings,
        telegram=tg,
        vector_store=None,
    )

    result = await _emit_agentic_mutate_if_any(
        ctx,
        "fg-inc-003",
        _siem_batch("inc-003"),
        sanitized_text="Some SIEM evidence.",
    )

    assert result is True, "SIEM path handled (True) even without admin_cid — prevents RAG_MISS fallthrough"
    tg.send_message.assert_not_called()
    # SUGGEST_REMEDIATION still emitted to Kafka
    assert any(t == "omni-actions" for t, _ in kafka.sent)
