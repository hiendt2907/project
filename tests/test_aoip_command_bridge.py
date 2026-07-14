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
