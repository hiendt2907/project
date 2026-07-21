"""Phase 4 (0-6 roadmap) — the diagnosis -> automated dispatch bridge.

This module deliberately does not decide safety policy (tier/risk/mutation
toggle) — the gateway does, via Phase 2's tier_gate wiring. These tests
cover what THIS module is responsible for: correctly recognizing when a
diagnosis is eligible for auto-dispatch, correctly shaping the advisory dict,
and correctly reporting why it did or didn't dispatch — never silently.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.auto_recovery_bridge import (
    build_dispatch_advisory,
    dispatch_if_eligible,
    extract_suggested_recovery,
)


class TestExtractSuggestedRecovery:
    def test_none_when_field_absent(self):
        assert extract_suggested_recovery({"root_cause": "x", "confidence": 0.9}) is None

    def test_none_when_field_is_null(self):
        assert extract_suggested_recovery({"suggested_recovery": None}) is None

    def test_none_when_capability_unsupported(self):
        final = {"suggested_recovery": {"capability": "k8s.delete_pod", "unit": "x"}}
        assert extract_suggested_recovery(final) is None

    def test_none_when_unit_empty(self):
        final = {"suggested_recovery": {"capability": "systemd.restart_unit", "unit": ""}}
        assert extract_suggested_recovery(final) is None

    def test_valid_extraction(self):
        final = {"suggested_recovery": {"capability": "systemd.restart_unit",
                                        "unit": "payment-api.service"}}
        assert extract_suggested_recovery(final) == {
            "capability": "systemd.restart_unit", "unit": "payment-api.service",
        }

    def test_bare_unit_name_normalized_with_service_suffix(self):
        """The LLM copies the unit verbatim from evidence facts, which store
        the bare name (systemd's own list-units strips the suffix) — the
        allowlist executor needs the full name, so this module normalizes it
        downstream of grounding, not the prompt/LLM."""
        final = {"suggested_recovery": {"capability": "systemd.restart_unit",
                                        "unit": "payment-api"}}
        assert extract_suggested_recovery(final) == {
            "capability": "systemd.restart_unit", "unit": "payment-api.service",
        }


class TestBuildDispatchAdvisory:
    def test_shapes_advisory_correctly(self):
        final = {"root_cause": "payment-api.service is down", "confidence": 0.9}
        suggested = {"capability": "systemd.restart_unit", "unit": "payment-api.service"}
        advisory = build_dispatch_advisory(
            final=final, suggested=suggested,
            mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
        )
        assert advisory["capability"] == "systemd.restart_unit"
        assert advisory["unit"] == "payment-api.service"
        assert advisory["summary"] == "payment-api.service is down"
        assert advisory["confidence"] == 0.9
        assert advisory["evidence_refs"] == ["diagnosis_session:inc-1"]

    def test_falls_back_to_generic_summary_when_root_cause_empty(self):
        advisory = build_dispatch_advisory(
            final={"confidence": 0.9},
            suggested={"capability": "systemd.restart_unit", "unit": "redis-server"},
            mission_id="m", decision_id="d", incident_id="i",
        )
        assert "redis-server" in advisory["summary"]


class TestDispatchIfEligible:
    async def test_skips_when_no_suggested_recovery(self):
        result = await dispatch_if_eligible(
            settings=SimpleNamespace(), http_client=AsyncMock(),
            final={"root_cause": "x", "confidence": 0.9},
            agent_id="a-1", tenant_id="t-1", trace_id="tr-1",
        )
        assert result == {"dispatched": False, "reason": "no_suggested_recovery",
                          "command_id": None, "state": None}

    async def test_skips_when_confidence_below_threshold(self):
        final = {"confidence": 0.5,
                "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "x.service"}}
        result = await dispatch_if_eligible(
            settings=SimpleNamespace(), http_client=AsyncMock(),
            final=final, agent_id="a-1", tenant_id="t-1", trace_id="tr-1",
        )
        assert result["dispatched"] is False
        assert result["reason"] == "confidence_below_threshold"

    async def test_skips_when_gateway_api_key_not_configured(self):
        final = {"confidence": 0.95, "root_cause": "x down",
                "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "x.service"}}
        result = await dispatch_if_eligible(
            settings=SimpleNamespace(omni_gateway_api_key=""), http_client=AsyncMock(),
            final=final, agent_id="a-1", tenant_id="t-1", trace_id="tr-1",
        )
        assert result["dispatched"] is False
        assert result["reason"] == "gateway_api_key_not_configured"

    async def test_dispatches_and_posts_to_real_enqueue_endpoint_shape(self):
        """Proves the HTTP call targets the correct endpoint with the correct
        auth header and a payload aoip.command_bridge actually produces —
        without a live gateway (that is the E2E drill's job)."""
        final = {"confidence": 0.95, "root_cause": "payment-api.service is down",
                "suggested_recovery": {"capability": "systemd.restart_unit",
                                       "unit": "payment-api.service"}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"command_id": "cmd-x", "state": "QUEUED"}
        client = AsyncMock()
        client.post = AsyncMock(return_value=mock_resp)

        settings = SimpleNamespace(
            omni_gateway_api_key="test-key",
            omni_gateway_internal_url="http://omni-gateway.multi-agent.svc.cluster.local:80",
        )
        result = await dispatch_if_eligible(
            settings=settings, http_client=client, final=final,
            agent_id="staging-sim_cust-app", tenant_id="staging-sim", trace_id="tr-real-1",
        )

        assert result["dispatched"] is True
        assert result["state"] == "QUEUED"
        client.post.assert_awaited_once()
        call = client.post.call_args
        assert call.args[0] == "http://omni-gateway.multi-agent.svc.cluster.local:80/webhook/agent/rt/commands/enqueue"
        assert call.kwargs["headers"] == {"Authorization": "Bearer test-key"}
        payload = call.kwargs["json"]
        assert payload["agent_id"] == "staging-sim_cust-app"
        assert payload["tenant_id"] == "staging-sim"
        assert payload["payload"]["capability"] == "systemd.restart_unit"
        assert payload["payload"]["target"]["unit"] == "payment-api.service"
        assert payload["payload"]["approval"]["approver"] == "auto-recovery:diagnosis_loop"

    async def test_reports_gateway_rejection_without_raising(self):
        """A 423 from the gateway's tier_gate (Phase 2) is a normal, expected
        outcome for a diagnosis that isn't auto-executable at the tenant's
        current tier — not an error this module should raise on."""
        final = {"confidence": 0.95, "root_cause": "x down",
                "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "x.service"}}
        mock_resp = MagicMock()
        mock_resp.status_code = 423
        mock_resp.json.return_value = {"detail": {"reason": "tier_gate_suggest", "tier": "shadow"}}
        client = AsyncMock()
        client.post = AsyncMock(return_value=mock_resp)
        settings = SimpleNamespace(omni_gateway_api_key="k", omni_gateway_internal_url="http://gw")

        result = await dispatch_if_eligible(
            settings=settings, http_client=client, final=final,
            agent_id="a-1", tenant_id="t-1", trace_id="tr-1",
        )
        assert result["dispatched"] is False
        assert result["http_status"] == 423
        assert result["gateway_detail"]["detail"]["reason"] == "tier_gate_suggest"
