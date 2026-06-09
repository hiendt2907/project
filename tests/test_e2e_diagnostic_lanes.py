"""
E2E tests for Lane 3 (APP_HTTP) and Lane 4 (SIEM_SECURITY) diagnostic lanes.

Coverage:
  C. Business error lane: 429 rate-limit → sigma bypass; 499 client_abort → no bypass; 401/403 auth → sigma bypass
  D. Smart-SIEM lane: diagnosis has WHAT/WHO/WHY/HOW-TO/Forecast; Telegram card has forecast; all 5 timeframes
  E. Edge cases: injection attack in namespace, unknown SIEM category, mixed error classes

Lane 1 (SYS_RESOURCE) and Lane 2 (SYS_HARD_FAIL) tests are in tests/e2e/.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from workers.evidence_consumer import (
    _emit_agentic_mutate_if_any,
    _siem_diagnosis_from_batch,
    _siem_forecast_timeline,
    _notify_siem_telegram,
)
from workers.log_surge_probe import (
    AccessErrorCounts,
    LogSurgeResult,
    classify_http_status,
    count_access_errors,
    evaluate_log_surge_sigma_bypass,
)

_EPHEMERAL_K8S_NS = (
    (os.environ.get("PYTEST_OMNI_K8S_NAMESPACE") or "").strip()
    or f"k8s-{uuid.uuid4().hex[:12]}"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ephemeral_telegram_chat_id() -> int:
    raw = (os.environ.get("OMNI_TEST_TELEGRAM_CHAT_ID") or "").strip()
    if raw:
        return int(raw)
    return random.randrange(10**8, 10**9)


def _ephemeral_loki_url() -> str:
    v = (os.environ.get("OMNI_TEST_LOKI_URL") or "").strip()
    return v or f"http://{uuid.uuid4().hex}.invalid"


def _make_settings(**overrides):
    base = {
        "omni_siem_suggest_only": True,
        "omni_auto_execute_enabled": False,
        "trace_correlation_ping_enabled": True,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_audit_chain": "omni-audit-chain",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "omni_llm_first_autonomy_enabled": False,
        "omni_unrestricted_tool_execution": False,
        "omni_legacy_deterministic_fallback": False,
        "omni_planner_precondition_gate_enabled": False,
        "telegram_admin_chat_id": _ephemeral_telegram_chat_id(),
        "omni_sigma_log_bypass_enabled": True,
        "omni_loki_base_url": _ephemeral_loki_url(),
        "omni_log_surge_window_sec": 300,
        "omni_log_surge_min_lines": 5,
        "omni_log_surge_min_ratio": 0.5,
        "omni_log_surge_line_limit": 500,
        "omni_log_surge_http_timeout_sec": 5.0,
        "baseline_dr_z_threshold": 3.0,
        "autonomous_sigma_observation_window": 1,
        "omni_proof_lane_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _KafkaCapture:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))


def _siem_batch(
    category: str = "ddos",
    severity: str = "critical",
    ns: str | None = None,
    incident_id: str | None = None,
    affected_ip: str | None = None,
) -> list[dict]:
    ns = ns or f"ns-{uuid.uuid4().hex[:10]}"
    incident_id = incident_id or f"inc-{uuid.uuid4().hex[:12]}"
    affected_ip = affected_ip or ".".join(str(random.randint(0, 255)) for _ in range(4))
    labels = {
        "alertname": f"SIEM{category.title()}",
        "siem_source": "finguard",
        "siem_category": category,
        "siem_incident_id": incident_id,
        "severity": severity,
        "namespace": ns,
    }
    return [{
        "probe": "siem_incident_context",
        "evidence_source": "SIEM",
        "alert_rule": f"SIEM{category.title()}",
        "alert_hint": f"SIEM alert category={category}",
        "canonical_query_snippet": json.dumps({"labels": labels}),
        "extracted_fact": {
            "category": category,
            "severity": severity,
            "incident_id": incident_id,
            "tenant": f"tenant-{uuid.uuid4().hex[:8]}",
            "description": f"{category.upper()} attack from {affected_ip}",
            "suggested_action": "Isolate and investigate",
            "affected_ip": affected_ip,
            "namespace": ns,
        },
        "result": "SIEM_INCIDENT",
        "symptom_group": "siem_incident",
    }]


def _access_lines(entries: list[tuple[int, int]]) -> list[str]:
    client = ".".join(str(random.randint(1, 223)) for _ in range(4))
    lines = []
    for status, count in entries:
        for _ in range(count):
            lines.append(
                f'{client} - - [01/Jan/2024] "GET /api HTTP/1.1" {status} 100'
            )
    return lines


# ===========================================================================
# C. Business error lane — 429/499/401/403 E2E classification
# ===========================================================================

@pytest.mark.asyncio
async def test_business_error_429_triggers_sigma_bypass():
    """429 rate-limit surge → sigma bypass OK with dominant_error_class=rate_limit."""
    lines = _access_lines([(429, 8), (200, 2)])  # 80% 429 — above min_ratio=0.5

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc-abc12",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is True, f"Expected sigma bypass for 429: reason={result.reason}"
    assert result.dominant_error_class == "rate_limit"
    assert result.reason == "access_rate_limit_sustained"
    assert result.meta["access_rate_limit_429"] == 8


@pytest.mark.asyncio
async def test_business_error_499_does_not_bypass_sigma():
    """499 client_abort → informational only, no sigma bypass."""
    lines = _access_lines([(499, 8), (200, 2)])

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc-abc12",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is False, "499 client_abort must NOT trigger sigma bypass"
    assert result.dominant_error_class == "client_abort"
    assert result.reason == "access_client_abort_informational"
    assert "not a server error" in (result.meta.get("note") or "")


@pytest.mark.asyncio
async def test_business_error_401_triggers_sigma_bypass():
    """401 auth failure surge → sigma bypass OK with dominant_error_class=auth_failure."""
    lines = _access_lines([(401, 6), (200, 4)])  # 60% 401

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc-abc12",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is True, f"Expected sigma bypass for 401: reason={result.reason}"
    assert result.dominant_error_class == "auth_failure"
    assert result.meta["access_auth_failure_401_403"] == 6


@pytest.mark.asyncio
async def test_business_error_403_triggers_sigma_bypass():
    """403 forbidden → sigma bypass OK."""
    lines = _access_lines([(403, 7), (200, 3)])

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc-abc12",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is True, f"Expected sigma bypass for 403: reason={result.reason}"
    assert result.dominant_error_class == "auth_failure"


@pytest.mark.asyncio
async def test_business_error_500_still_bypasses():
    """5xx baseline path still works after refactor."""
    lines = _access_lines([(500, 6), (503, 2), (200, 2)])

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc-abc12",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is True
    assert result.dominant_error_class == "5xx"


@pytest.mark.asyncio
async def test_business_error_loki_unavailable_escalates():
    """Loki connection error → escalate_log_unavailable=True."""
    async def fake_loki(*args, **kwargs):
        return [], "connection refused"

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is False
    assert result.escalate_log_unavailable is True


@pytest.mark.asyncio
async def test_business_error_mixed_5xx_and_429_5xx_wins():
    """When both 5xx and 429 are present but 5xx is dominant, 5xx class wins (checked first)."""
    # 5xx: 6/10=60%, 429: 3/10=30% — 5xx checked first
    lines = _access_lines([(500, 6), (429, 3), (200, 1)])

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is True
    assert result.dominant_error_class == "5xx"


# ===========================================================================
# D. Smart-SIEM lane — structured advisory + forecast E2E
# ===========================================================================

@pytest.mark.parametrize("category,severity", [
    ("ddos", "critical"),
    ("malware", "critical"),
    ("data_exfil", "critical"),
    ("k8s_threat", "critical"),
    ("auth_failure", "high"),
    ("lateral_movement", "critical"),
    ("network_anomaly", "critical"),
    ("unknown_threat", "critical"),  # edge: unknown category → default
])
def test_siem_lane_diagnosis_all_sections_present(category: str, severity: str) -> None:
    """All SIEM categories must have WHAT/WHO/WHY/HOW-TO/Forecast sections."""
    batch = _siem_batch(category=category, severity=severity)
    labels = {
        "siem_category": category,
        "severity": severity,
        "namespace": _EPHEMERAL_K8S_NS,
        "siem_incident_id": f"i-{uuid.uuid4().hex[:10]}",
    }
    diag = _siem_diagnosis_from_batch(batch, labels, "")

    assert "WHAT:" in diag, f"[{category}] WHAT section missing"
    assert "WHO:" in diag, f"[{category}] WHO section missing"
    assert "WHY:" in diag, f"[{category}] WHY section missing"
    assert "HOW-TO" in diag, f"[{category}] HOW-TO section missing"
    assert "Forecast" in diag, f"[{category}] Forecast section missing"
    assert "+1h" in diag, f"[{category}] 1h forecast missing"
    assert "+24h" in diag, f"[{category}] 24h forecast missing"


@pytest.mark.asyncio
async def test_siem_lane_telegram_card_has_forecast():
    """Telegram card for SIEM must include forecast section."""
    _tg = f"e2e-tg-{uuid.uuid4().hex[:10]}"
    batch = _siem_batch(category="ddos", severity="critical", ns=_EPHEMERAL_K8S_NS)
    labels = {
        "siem_category": "ddos",
        "severity": "critical",
        "namespace": _EPHEMERAL_K8S_NS,
        "siem_incident_id": _tg,
    }
    diag = _siem_diagnosis_from_batch(batch, labels, "")

    tg_captures = []
    tg = MagicMock()
    tg.send_message = AsyncMock(side_effect=lambda cid, msg: tg_captures.append(msg))

    settings = _make_settings()
    ctx = SimpleNamespace(settings=settings, telegram=tg)
    await _notify_siem_telegram(ctx, trace=_tg, batch=batch, diagnosis=diag)

    assert tg_captures, "Expected Telegram message"
    card = tg_captures[0]
    assert "Forecast" in card, f"Forecast missing in card: {card[:400]}"
    assert "+1h" in card or "1h" in card, "1h horizon missing in card"
    assert "CRITICAL" in card or "CATASTROPHIC" in card, "Severity missing in card"


@pytest.mark.asyncio
async def test_siem_lane_emit_agentic_includes_howto():
    """_emit_agentic_mutate_if_any for SIEM must emit SUGGEST_REMEDIATION with HOW-TO in diagnosis."""
    _emit_trace = f"e2e-emit-{uuid.uuid4().hex[:10]}"
    _emit_ns = f"red-{uuid.uuid4().hex[:10]}"
    batch = _siem_batch(category="malware", severity="critical", ns=_emit_ns)
    labels = {
        "siem_category": "malware",
        "severity": "critical",
        "namespace": _emit_ns,
        "siem_incident_id": _emit_trace,
    }

    kafka = _KafkaCapture()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tg = MagicMock()
    tg.send_message = AsyncMock()

    settings = _make_settings(omni_siem_suggest_only=True)
    ctx = SimpleNamespace(settings=settings, kafka=kafka, redis=redis, telegram=tg,
                          vector_store=None, inbound_trace_id="", scout_ready=MagicMock(is_set=MagicMock(return_value=True)))

    with patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock):
        await _emit_agentic_mutate_if_any(
            ctx,
            trace=_emit_trace,
            batch=batch,
            sanitized_text="",
            rag_match_text=None,
            attempt_count=1,
        )

    # Verify SUGGEST_REMEDIATION was emitted
    topics = [t for t, _ in kafka.sent]
    assert "omni-actions" in topics, f"omni-actions not emitted: {kafka.sent}"

    # Verify the diagnosis content
    action_payload = next((p for t, p in kafka.sent if t == "omni-actions"), None)
    assert action_payload is not None
    outer = json.loads(action_payload["data"])
    # build_suggest_remediation_body wraps diagnosis inside {"action":..., "data": {"diagnosis": ...}}
    diag = outer.get("data", {}).get("diagnosis", "")
    assert "HOW-TO" in diag or "kubectl" in diag, f"HOW-TO/kubectl missing in diagnosis: {diag[:400]}"


@pytest.mark.asyncio
async def test_siem_lane_injection_attack_in_namespace():
    """Namespace with format-string injection must not cause errors."""
    evil_ns = "ns-{evil_injection}"
    batch = _siem_batch(category="ddos", ns=evil_ns)
    labels = {
        "siem_category": "ddos",
        "severity": "critical",
        "namespace": evil_ns,
        "siem_incident_id": f"e2e-inject-{uuid.uuid4().hex[:10]}",
    }

    # Must not raise
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "HOW-TO" in diag
    # No braces in HOW-TO section (injected and stripped)
    howto_section = diag.split("HOW-TO", 1)[1].split("Forecast", 1)[0]
    assert "{" not in howto_section, f"Braces leaked into HOW-TO: {howto_section}"


def test_siem_lane_all_forecasts_have_five_horizons() -> None:
    """Every category × severity combo must produce exactly 5 forecast horizons."""
    categories = ["ddos", "malware", "data_exfil", "k8s_threat", "auth_failure",
                  "lateral_movement", "network_anomaly", "unknown_category"]
    severities = ["critical", "high", "medium", "low"]
    expected = {"1h", "3h", "6h", "12h", "24h"}

    failures = []
    for cat in categories:
        for sev in severities:
            forecast = _siem_forecast_timeline(cat, sev)
            tfs = {f["timeframe"] for f in forecast}
            if tfs != expected:
                failures.append(f"{cat}/{sev}: got {tfs}")

    assert not failures, "Missing timeframes:\n" + "\n".join(failures)


# ===========================================================================
# E. Edge cases
# ===========================================================================

def test_edge_empty_batch_no_crash() -> None:
    """Empty batch must not crash _siem_diagnosis_from_batch."""
    diag = _siem_diagnosis_from_batch([], {}, "")
    assert isinstance(diag, str)
    assert len(diag) > 0


def test_edge_forecast_unknown_category_uses_default() -> None:
    """Unknown category must fall back to default 5-timeframe forecast."""
    forecast = _siem_forecast_timeline("completely_unknown_category_xyz", "critical")
    assert len(forecast) == 5
    tfs = {f["timeframe"] for f in forecast}
    assert tfs == {"1h", "3h", "6h", "12h", "24h"}


@pytest.mark.parametrize("status,expected_class", [
    (500, "5xx"), (501, "5xx"), (502, "5xx"), (503, "5xx"), (504, "5xx"),
    (429, "rate_limit"),
    (499, "client_abort"),
    (401, "auth_failure"), (403, "auth_failure"),
    (200, "ok"), (301, "ok"), (404, "ok"), (304, "ok"),
])
def test_edge_classify_all_statuses(status: int, expected_class: str) -> None:
    assert classify_http_status(status) == expected_class


@pytest.mark.asyncio
async def test_edge_mixed_errors_below_threshold_no_bypass() -> None:
    """Mixed errors below min_ratio=0.5 → no sigma bypass."""
    # 3/10 5xx, 2/10 429, 1/10 401 — none above 50%
    lines = _access_lines([(500, 3), (429, 2), (401, 1), (200, 4)])

    async def fake_loki(*args, **kwargs):
        return lines, ""

    with patch("workers.log_surge_probe.loki_query_range_lines", side_effect=fake_loki):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=_ephemeral_loki_url(),
            namespace=_EPHEMERAL_K8S_NS,
            pod_name="api-svc",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )

    assert result.ok is False
    assert result.reason == "insufficient_error_evidence"


# ---------------------------------------------------------------------------
