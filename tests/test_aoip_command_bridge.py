import pytest


def test_advisory_to_durable_command_requires_grounded_diagnosis():
    from aoip.command_bridge import build_durable_command

    advisory = {
        "mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
        "capability": "systemd.restart_unit", "unit": "nginx.service",
        "summary": "nginx is down", "confidence": 0.91,
        "evidence_refs": ["probe:systemctl:1"],
    }
    command = build_durable_command(
        advisory, tenant="acme", agent_id="agent-1", approver="alice", now=100.0, ttl_s=300
    )

    assert command["agent_id"] == "agent-1"
    assert command["tenant_id"] == "acme"
    assert command["payload"]["capability"] == "systemd.restart_unit"
    assert command["payload_hash"]
    assert command["payload"]["evidence"]["findings"]


@pytest.mark.parametrize("missing", ["mission_id", "decision_id", "incident_id", "evidence_refs"])
def test_advisory_bridge_fails_closed_without_required_grounding(missing):
    from aoip.command_bridge import build_durable_command

    advisory = {
        "mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
        "capability": "systemd.restart_unit", "unit": "nginx.service",
        "summary": "nginx is down", "confidence": 0.91,
        "evidence_refs": ["probe:systemctl:1"],
    }
    advisory.pop(missing)
    with pytest.raises(ValueError, match="grounded|required"):
        build_durable_command(
            advisory, tenant="acme", agent_id="agent-1", approver="alice", now=100.0, ttl_s=300
        )


def test_durable_command_payload_decodes_via_the_deployed_daemons_actual_decoder():
    """Regression for a live defect found 2026-07-20: the deployed VM daemon's
    configured executor is operations.build_recovery_executor, whose
    decode_recovery_command() requires payload["recovery"] — but this bridge
    (feeding the operator CLI / future Phase 4 auto-dispatch) only produced
    the capability/target/reason shape. A real drill enqueued through the
    real CLI failed decode entirely before this fix. Both decoders must
    accept the same payload now — no manual translation needed at dispatch."""
    from aoip.agent.operations import decode_recovery_command
    from aoip.capabilities.systemd_restart import _decode as capability_decode
    from aoip.command_bridge import build_durable_command

    advisory = {
        "mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
        "capability": "systemd.restart_unit", "unit": "nginx.service",
        "summary": "nginx is down", "confidence": 0.91,
        "evidence_refs": ["probe:systemctl:1"],
    }
    command = build_durable_command(
        advisory, tenant="acme", agent_id="agent-1", approver="alice", now=100.0, ttl_s=300
    )
    payload = command["payload"]

    # Stack B decoder (the one the live daemon actually uses)
    req, approval, ctx = decode_recovery_command(payload)
    assert req.unit == "nginx.service"
    assert req.failure_mode == "process_down"
    assert req.substrate == "systemd"
    assert req.tenant == "acme"
    assert approval.approved is True
    assert ctx.findings
    # mission/incident/decision_id must survive into the recovery section too —
    # without them _key_for()'s correlation-based idempotency key silently
    # degrades to a coarser one (caught live 2026-07-21).
    assert req.mission_id == "m1"
    assert req.incident_id == "i1"
    assert req.decision_id == "d1"

    # Stack A decoder (the capability-specific CLI/console path) must still work too
    req2, approval2, ctx2, preflight_cfg = capability_decode(payload, tenant="acme")
    assert req2.unit == "nginx.service"
    assert approval2.approved is True
