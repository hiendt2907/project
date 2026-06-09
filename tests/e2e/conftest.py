"""Shared fixtures and helpers for Lane 1+2 E2E tests."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs: object) -> None:
        # Real KafkaBus.send_dict accepts a `key=` (audit-chain compact topic);
        # mirror that signature so the CRAT write path works in tests.
        self.sent.append((topic, payload))

    def get(self, topic: str) -> list[dict]:
        return [payload for t, payload in self.sent if t == topic]

    def assert_sent(self, topic: str, count: int = 1) -> None:
        msgs = self.get(topic)
        assert len(msgs) == count, (
            f"Expected {count} message(s) on '{topic}', got {len(msgs)}: {msgs}"
        )


def make_settings(**overrides: object) -> SimpleNamespace:
    base: dict = {
        "omni_siem_suggest_only": True,
        "omni_auto_execute_enabled": False,
        "trace_correlation_ping_enabled": False,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "omni_llm_first_autonomy_enabled": False,
        "omni_unrestricted_tool_execution": False,
        "omni_legacy_deterministic_fallback": False,
        "omni_planner_precondition_gate_enabled": False,
        "omni_sigma_log_bypass_enabled": True,
        "omni_proof_lane_enabled": True,
        "baseline_dr_z_threshold": 3.0,
        "kafka_topic_audit_chain": "omni-audit-chain",
        "autonomous_sigma_observation_window": 1,
        "telegram_admin_chat_id": 123456789,
        "omni_loki_base_url": f"http://{uuid.uuid4().hex}.invalid",
        "omni_log_surge_window_sec": 300,
        "omni_log_surge_min_lines": 5,
        "omni_log_surge_min_ratio": 0.5,
        "omni_log_surge_line_limit": 500,
        "omni_log_surge_http_timeout_sec": 5.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_ctx(
    redis: object = None,
    kafka: object = None,
    settings: object = None,
    telegram: object = None,
    **extra: object,
) -> SimpleNamespace:
    r = redis if redis is not None else fakeredis.aioredis.FakeRedis(decode_responses=True)
    k = kafka if kafka is not None else _KafkaCapture()
    s = settings if settings is not None else make_settings()
    return SimpleNamespace(
        settings=s,
        kafka=k,
        redis=r,
        telegram=telegram,
        vector_store=None,
        inbound_trace_id="",
        scout_ready=MagicMock(is_set=MagicMock(return_value=True)),
        **extra,
    )


def make_resource_ev(trace: str = "trace-lane1-e2e") -> dict:
    return {
        "probe": "node_cpu_saturation",
        "kind": "diagnostic_evidence",
        "trace_id": trace,
        "alert_hint": "CPU saturation",
        "symptom_group": "workload_resource",
        "layer": "prometheus",
        "result": "PASSED",
        "extracted_fact": json.dumps({"s0": 0.95, "unit": "saturation_ratio"}),
        "canonical_query_snippet": json.dumps({
            "labels": {"alertname": "HighCPU", "namespace": "multi-agent", "deployment": "api-svc"}
        }),
    }


def make_advisory(trace_id: str = "trace-test") -> object:
    """Build a minimal valid AnalystAdvisory for use in test mocks."""
    from pkg.reasoning.analyst_advisory_schema import (
        AnalystAdvisory,
        ForecastTimeline,
        ProposedRemediationStep,
        VerificationStep,
    )
    return AnalystAdvisory(
        trace_id=trace_id,
        verdict="URGENT",
        confidence="high",
        root_cause="CPU saturation detected z=4.5 on api-svc",
        affected_workload="multi-agent/api-svc",
        verification_steps=[
            VerificationStep(order=1, command="kubectl top pod -n multi-agent", rationale="verify CPU usage")
        ],
        proposed_remediation=[
            ProposedRemediationStep(order=1, action="kubectl scale deployment api-svc --replicas=3", approval_required=True)
        ],
        forecast=ForecastTimeline(method="heuristic", forecasts=[]),
    )


# ── pytest fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def kafka_capture() -> _KafkaCapture:
    return _KafkaCapture()
