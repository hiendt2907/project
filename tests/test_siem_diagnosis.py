"""Tests for SIEM incident-specific diagnosis: _siem_diagnosis_from_batch + Telegram card."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from workers.evidence_consumer import _siem_diagnosis_from_batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _siem_batch(category: str = "ddos", severity: str = "critical", ns: str = "multi-agent") -> list[dict]:
    siem_labels = {
        "alertname": f"SIEM{category.title()}",
        "siem_source": "finguard",
        "siem_category": category,
        "siem_incident_id": "inc-test-001",
        "severity": severity,
        "namespace": ns,
    }
    return [{
        "probe": "siem_incident_context",
        "evidence_source": "SIEM",
        "alert_rule": f"SIEM{category.title()}",
        "alert_hint": f"SIEM alert category={category}",
        "canonical_query_snippet": json.dumps({"labels": siem_labels}),
        "extracted_fact": {
            "category": category,
            "severity": severity,
            "incident_id": "inc-test-001",
            "tenant": "tenant-1",
            "description": f"Large-scale {category} attack detected",
            "suggested_action": f"Block and investigate",
            "affected_ip": "10.0.1.5",
            "namespace": ns,
        },
        "namespace": ns,
        "deployment": "",
        "pod": "",
        "result": "SIEM_INCIDENT",
        "raw": "",
        "ts": "1700000000",
        "layer": "security",
        "symptom_group": "siem_incident",
        "clinical_priority_note": "Primary: FinGuard/Smart-SIEM real-time security incident.",
    }]


# ---------------------------------------------------------------------------
# A) _siem_diagnosis_from_batch
# ---------------------------------------------------------------------------

def test_diag_contains_incident_id():
    batch = _siem_batch(category="ddos")
    siem_labels = {"siem_incident_id": "inc-test-001", "siem_category": "ddos", "severity": "critical"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "inc-test-001" in diag


def test_diag_contains_category_and_namespace():
    batch = _siem_batch(category="malware", ns="prod-ns")
    siem_labels = {"siem_category": "malware", "severity": "high", "namespace": "prod-ns"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "malware" in diag
    assert "prod-ns" in diag


def test_diag_contains_affected_ip():
    batch = _siem_batch(category="ddos")
    siem_labels = {"siem_category": "ddos", "severity": "critical", "namespace": "multi-agent"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "10.0.1.5" in diag


def test_diag_contains_description():
    batch = _siem_batch(category="data_exfil")
    siem_labels = {"siem_category": "data_exfil", "severity": "critical"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "data_exfil" in diag.lower() or "exfil" in diag.lower()
    assert "attack detected" in diag.lower() or "large-scale" in diag.lower()


def test_diag_ddos_has_network_steps():
    batch = _siem_batch(category="ddos", ns="frontend")
    siem_labels = {"siem_category": "ddos", "severity": "critical", "namespace": "frontend"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "networkpolicy" in diag.lower() or "ingress" in diag.lower()
    assert "frontend" in diag


def test_diag_has_verify_block_before_howto():
    """F6: scope-confirmation VERIFY block precedes the cluster-scoped HOW-TO."""
    batch = _siem_batch(category="ddos", ns="frontend")
    siem_labels = {"siem_category": "ddos", "severity": "critical", "namespace": "frontend"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "VERIFY FIRST" in diag
    assert diag.index("VERIFY FIRST") < diag.index("HOW-TO")


# ---------------------------------------------------------------------------
# F6-extra: principle-based reasoning — behavioral, not literal-string matching.
# Each test asserts a *property of the conclusion* (scope correctness, no
# over-claim, no dead-end) that must hold for ANY IP/category, not canned text.
# ---------------------------------------------------------------------------

def _set_sources(batch: list[dict], ips: list[str]) -> None:
    """Override the source addresses the evidence carries."""
    ef = batch[0]["extracted_fact"]
    ef["affected_ip"] = ips[0] if ips else ""
    ef["source_ips"] = list(ips)


def test_diag_single_internal_source_not_distributed():
    """RFC1918 single source → investigate internal host, never edge-block, never 'distributed'."""
    batch = _siem_batch(category="ddos")
    _set_sources(batch, ["10.0.0.42"])  # RFC1918
    diag = _siem_diagnosis_from_batch(batch, {"siem_category": "ddos", "severity": "critical"}, "").lower()
    assert "single internal source" in diag
    assert "not a distributed external attack" in diag
    assert "do not edge-block" in diag


def test_diag_single_public_source_no_internal_claim():
    """Genuinely public single source → external framing, must NOT claim internal/distributed."""
    batch = _siem_batch(category="ddos")
    _set_sources(batch, ["8.8.8.8"])  # genuinely public
    diag = _siem_diagnosis_from_batch(batch, {"siem_category": "ddos", "severity": "critical"}, "")
    why = diag.split("VERIFY FIRST")[0].lower()  # the load-bearing scope claim
    assert "single internal source" not in why
    assert "single external source" in why
    # confirm-ingress principle present in VERIFY
    assert "reach" in diag.lower() and "cluster" in diag.lower()


def test_diag_unknown_category_unknown_ip_no_dead_end():
    """Category never in any table + no source IP → still reasons, never an empty/default lookup."""
    batch = _siem_batch(category="zero_day_xyz")
    _set_sources(batch, [])  # no origin captured
    diag = _siem_diagnosis_from_batch(batch, {"siem_category": "zero_day_xyz", "severity": "critical"}, "")
    low = diag.lower()
    assert len(diag) > 80
    # the principle for missing origin: identify source first, do not assume scope
    assert "unconfirmed" in low or "identify the source" in low
    assert "verify first" in low  # VERIFY block always produced, never empty
    # no canned "anomaly confidence exceeds threshold" default boilerplate
    assert "anomaly confidence exceeds" not in low


def test_diag_multiple_external_sources_is_distributed():
    """Several distinct public sources → distributed classification, NOT single-source."""
    batch = _siem_batch(category="ddos")
    _set_sources(batch, ["8.8.8.8", "1.1.1.1", "9.9.9.9"])
    diag = _siem_diagnosis_from_batch(batch, {"siem_category": "ddos", "severity": "critical"}, "").lower()
    assert "distributed" in diag
    assert "single" not in diag.split("verify first")[0]  # WHY part must not call it single
    assert "3 distinct source" in diag


def test_diag_multiple_internal_sources_is_lateral_not_flood():
    """Several internal sources → lateral/compromised-segment framing, not external flood."""
    batch = _siem_batch(category="network_anomaly")
    _set_sources(batch, ["10.0.0.5", "10.0.0.9", "172.16.4.2"])
    diag = _siem_diagnosis_from_batch(batch, {"siem_category": "network_anomaly", "severity": "high"}, "").lower()
    assert "multiple internal sources" in diag
    assert "external flood" not in diag.split("verify first")[0] or "not an external flood" in diag


def test_diag_blast_radius_from_evidence_pps():
    """Blast-radius line reflects observed pps from the evidence, not a hardcoded catastrophe."""
    batch = _siem_batch(category="ddos")
    _set_sources(batch, ["8.8.8.8"])
    batch[0]["raw"] = "inbound pps 80k from 8.8.8.8 — conntrack table filling"
    diag = _siem_diagnosis_from_batch(batch, {"siem_category": "ddos", "severity": "critical"}, "")
    assert "Blast-radius (from evidence)" in diag
    assert "80,000 pps" in diag


def test_diag_verify_always_non_empty_for_any_category():
    """VERIFY must generalise: every category (even unseen) yields concrete scope checks."""
    for cat in ("ddos", "totally_new_category", "", "auth_failure"):
        batch = _siem_batch(category=cat or "x")
        _set_sources(batch, ["8.8.8.8"])
        diag = _siem_diagnosis_from_batch(batch, {"siem_category": cat, "severity": "high"}, "")
        verify_section = diag.split("VERIFY FIRST")[1].split("HOW-TO")[0]
        # at least the origin-classify + confirm-ingress principles
        assert "Classify source" in verify_section
        assert "terminates inside the cluster" in verify_section


def test_diag_k8s_threat_has_rbac_steps():
    batch = _siem_batch(category="k8s_threat", ns="kube-system")
    siem_labels = {"siem_category": "k8s_threat", "severity": "critical", "namespace": "kube-system"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    assert "rbac" in diag.lower() or "clusterrolebinding" in diag.lower() or "admin" in diag.lower()


def test_diag_no_placeholder_brackets():
    """No <namespace> or <pod-name> placeholders — should use real values."""
    batch = _siem_batch(category="malware", ns="staging")
    siem_labels = {"siem_category": "malware", "severity": "high", "namespace": "staging"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    # <namespace> placeholder should be replaced by real ns
    assert "<namespace>" not in diag, f"Found <namespace> placeholder: {diag}"


def test_diag_unknown_category_falls_back_to_default():
    batch = _siem_batch(category="zero_day")
    siem_labels = {"siem_category": "zero_day", "severity": "critical", "namespace": "multi-agent"}
    diag = _siem_diagnosis_from_batch(batch, siem_labels, "")
    # Should still produce a non-empty diagnosis
    assert len(diag) > 50
    assert "zero_day" in diag or "multi-agent" in diag


# ---------------------------------------------------------------------------
# B) End-to-end: SIEM Telegram card has real incident data
# ---------------------------------------------------------------------------

def _make_ctx(telegram_captures: list, **overrides):
    settings_defaults = {
        "rag_truth_law_enforced": True,
        "trace_correlation_ping_enabled": True,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "model_reasoning_engine": "llama3",
        "diag_evidence_llm_model": "",
        "omni_siem_suggest_only": True,
        "omni_llm_first_autonomy_enabled": False,
        "omni_unrestricted_tool_execution": True,
        "omni_legacy_deterministic_fallback": False,
        "omni_planner_precondition_gate_enabled": False,
        "omni_auto_execute_enabled": False,
        "diag_k8s_expert_rag_enabled": False,
        "baseline_snapshot_enabled": False,
        "autonomous_agentic_max_steps": 3,
        "autonomous_sigma_observation_window": 1,
        "baseline_dr_z_threshold": 3.0,
        "omni_proof_lane_enabled": True,
        "rag_evidence_contradiction_check_enabled": False,
        "lab_chaos_credential_autofix_enabled": False,
        "omni_shadow_os_mode": False,
        "omni_sigma_log_bypass_enabled": False,
        "telegram_admin_chat_id": "99999",
        "kafka_topic_diagnostic_evidence": "omni-diagnostic-evidence",
        "kafka_topic_audit_chain": "omni-audit-chain",
    }
    settings_defaults.update(overrides)
    settings = SimpleNamespace(**settings_defaults)

    kafka = MagicMock()
    kafka.send_dict = AsyncMock()

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    tg = MagicMock()
    tg.send_message = AsyncMock(side_effect=lambda cid, txt: telegram_captures.append({"chat_id": cid, "text": txt}))

    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": (
        'MACHINE_JSON: {"verdict":"ESCALATE","hypothesis":"SIEM incident — no K8s identity",'
        '"action":{"tool":"","args":{}}}\n'
        "HUMAN_SUMMARY: SIEM security incident detected; human review required."
    )}})
    return SimpleNamespace(
        settings=settings,
        kafka=kafka,
        redis=redis,
        scout_ready=MagicMock(is_set=MagicMock(return_value=True)),
        llm=llm,
        telegram=tg,
        vector_store=None,
        inbound_trace_id="",
    )


@pytest.mark.asyncio
async def test_siem_telegram_has_real_incident_data():
    """SIEM Telegram card must contain real incident_id, category, affected_ip — not generic."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    tg_captures: list = []
    # Bypass advisory mode gate (requires siem_suggest_only=True AND NOT auto_execute_enabled).
    # Setting auto_execute_enabled=True disables the gate so the SIEM fast-path in
    # _emit_agentic_mutate_if_any (which still checks siem_suggest_only=True) runs normally.
    ctx = _make_ctx(tg_captures, omni_auto_execute_enabled=True)

    batch_item = _siem_batch(category="ddos", ns="prod-ns")[0]
    ev_doc = {**batch_item, "kind": "diagnostic_evidence", "trace_id": "fg-siem-tg-001"}

    import unittest.mock as mock
    with (
        mock.patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mb,
        mock.patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mm,
        mock.patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mg,
        mock.patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        mock.patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_suggest_remediation", new_callable=AsyncMock),
    ):
        mb.return_value = [ev_doc]
        mm.return_value = [ev_doc]
        gate_out = SimpleNamespace(hit=False, formatted="", best_score=None, match_text_en="", suggested_tool="", detail=None, chunk_ids=[])
        mg.return_value = gate_out
        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    assert tg_captures, "Expected at least one Telegram message"
    full_text = "\n".join(m["text"] for m in tg_captures)

    assert "inc-test-001" in full_text, f"incident_id missing: {full_text!r}"
    assert "ddos" in full_text.lower(), f"category missing: {full_text!r}"
    assert "prod-ns" in full_text, f"namespace missing: {full_text!r}"
    assert "10.0.1.5" in full_text, f"affected_ip missing: {full_text!r}"
    assert "Problem:" in full_text, f"Problem: section missing: {full_text!r}"
    assert "Advise:" in full_text, f"Advise: section missing: {full_text!r}"


@pytest.mark.asyncio
async def test_siem_telegram_no_generic_placeholders():
    """SIEM Telegram must not contain <namespace> or <pod-name> placeholders."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    tg_captures: list = []
    ctx = _make_ctx(tg_captures)

    batch_item = _siem_batch(category="malware", ns="blue-ns")[0]
    ev_doc = {**batch_item, "kind": "diagnostic_evidence", "trace_id": "fg-siem-tg-002"}

    import unittest.mock as mock
    with (
        mock.patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mb,
        mock.patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mm,
        mock.patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mg,
        mock.patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        mock.patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_suggest_remediation", new_callable=AsyncMock),
    ):
        mb.return_value = [ev_doc]
        mm.return_value = [ev_doc]
        gate_out = SimpleNamespace(hit=False, formatted="", best_score=None, match_text_en="", suggested_tool="", detail=None, chunk_ids=[])
        mg.return_value = gate_out
        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    full_text = "\n".join(m["text"] for m in tg_captures)
    assert "<namespace>" not in full_text, f"Generic placeholder found: {full_text!r}"
    assert "<pod-name>" not in full_text, f"Generic placeholder found: {full_text!r}"
