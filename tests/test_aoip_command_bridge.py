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


def test_stack_b_decoder_rejects_tampered_target_after_approval(monkeypatch):
    """Phase 3 (0-6 roadmap): decode_recovery_command() previously had NO
    payload-hash tamper-binding at all — only the CLI-only Stack A decoder
    did. A payload built via issue_capability_command(), then modified (e.g.
    a MITM or compromised transport swapping the target unit after approval
    was issued), used to decode and execute successfully through the live
    daemon's actual executor. Now both decoders share one hash definition
    (operations.capability_payload_hash) and both reject a mismatch."""
    from aoip.agent.operations import UnsupportedRecoveryPayload, decode_recovery_command
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
    tampered = dict(command["payload"])
    tampered["target"] = {"unit": "mariadb.service"}  # approved nginx, tampered to mariadb

    with pytest.raises(UnsupportedRecoveryPayload, match="payload_hash_mismatch"):
        decode_recovery_command(tampered)


def test_capability_hash_stable_across_diagnosed_at_float_jitter():
    """Regression for a live false-positive found 2026-07-21: a real,
    untampered payload from the real operator CLI failed hash verification
    end-to-end because reason.diagnosed_at (a full-precision Unix timestamp
    float) does not always round-trip to a byte-identical JSON string across
    the real transport chain (CLI's json.dumps -> gateway's Pydantic/
    pydantic-core -> Redis storage -> VM daemon's httpx) even though the
    underlying float value is unchanged. The hash must be robust to that
    while still catching a REAL tamper (target.unit changed)."""
    from aoip.agent.operations import capability_payload_hash

    base = {
        "capability": "systemd.restart_unit", "capability_version": "1",
        "target": {"unit": "payment-api.service"},
        "reason": {"mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
                  "summary": "x", "diagnosed_at": 1784597963.96865},
        "preconditions": {"require_unit_exists": True, "require_allowlisted": True},
        "verification": {"require_active_state": True, "health_check": None},
    }
    jittered = {**base, "reason": {**base["reason"], "diagnosed_at": 1784597963.968650001}}
    tampered = {**base, "target": {"unit": "mariadb.service"}}

    assert capability_payload_hash(base) == capability_payload_hash(jittered)
    assert capability_payload_hash(base) != capability_payload_hash(tampered)


def test_advisory_to_durable_command_routes_reset_failed_capability():
    """command_bridge must dispatch to the RIGHT typed adapter based on the
    advisory's declared capability, not just restart_unit — this is the
    parameterized-by-capability-name registry, not a hardcoded branch."""
    from aoip.agent.operations import decode_recovery_command
    from aoip.capabilities.systemd_reset_failed import _decode as reset_failed_decode
    from aoip.command_bridge import build_durable_command

    advisory = {
        "mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
        "capability": "systemd.reset_failed", "unit": "payment-api.service",
        "summary": "stuck failed, dependency now healthy", "confidence": 0.91,
        "evidence_refs": ["probe:systemctl:1"],
    }
    command = build_durable_command(
        advisory, tenant="acme", agent_id="agent-1", approver="alice", now=100.0, ttl_s=300
    )
    payload = command["payload"]
    assert payload["capability"] == "systemd.reset_failed"

    # Stack B decoder (the one the live daemon actually uses)
    req, approval, ctx = decode_recovery_command(payload)
    assert req.unit == "payment-api.service"
    assert req.failure_mode == "failed_state_stale"
    assert req.substrate == "systemd"
    assert approval.approved is True

    # Stack A decoder (capability-specific CLI/console path)
    req2, approval2, ctx2, preflight_cfg = reset_failed_decode(payload, tenant="acme")
    assert req2.unit == "payment-api.service"
    assert approval2.approved is True


def test_advisory_bridge_still_rejects_truly_unsupported_capability():
    from aoip.command_bridge import build_durable_command

    advisory = {
        "mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
        "capability": "k8s.delete_pod", "unit": "nginx.service",
        "summary": "x", "confidence": 0.91, "evidence_refs": ["probe:1"],
    }
    with pytest.raises(ValueError, match="unsupported capability"):
        build_durable_command(
            advisory, tenant="acme", agent_id="agent-1", approver="alice", now=100.0, ttl_s=300
        )


def test_advisory_to_durable_command_routes_journal_vacuum_capability():
    """command_bridge must dispatch capability #3 (journal_vacuum) to its OWN
    typed adapter, same registry mechanism proven for reset_failed above —
    not a hardcoded branch."""
    from aoip.agent.operations import decode_recovery_command
    from aoip.capabilities.systemd_journal_vacuum import _decode as journal_vacuum_decode
    from aoip.command_bridge import build_durable_command

    advisory = {
        "mission_id": "m1", "decision_id": "d1", "incident_id": "i1",
        "capability": "systemd.journal_vacuum", "unit": "systemd-journald.service",
        "summary": "journal disk usage 3.0G over threshold", "confidence": 0.91,
        "evidence_refs": ["probe:journalctl:1"],
    }
    command = build_durable_command(
        advisory, tenant="acme", agent_id="agent-1", approver="alice", now=100.0, ttl_s=300
    )
    payload = command["payload"]
    assert payload["capability"] == "systemd.journal_vacuum"

    # Stack B decoder (the one the live daemon actually uses)
    req, approval, ctx = decode_recovery_command(payload)
    assert req.unit == "systemd-journald.service"
    assert req.failure_mode == "disk_pressure_journal"
    assert req.substrate == "systemd"
    assert approval.approved is True

    # Stack A decoder (capability-specific CLI/console path)
    req2, approval2, ctx2, preflight_cfg = journal_vacuum_decode(payload, tenant="acme")
    assert req2.unit == "systemd-journald.service"
    assert approval2.approved is True


def test_stack_b_decoder_skips_hash_check_for_non_typed_payload():
    """A generic (non-capability) payload — the shape hand-authored tests
    throughout this session use, and the only shape possible before
    command_bridge/systemd_restart existed — has nothing typed to bind a
    hash to, so the check is skipped rather than failing closed on every
    legacy caller."""
    from aoip.agent.operations import decode_recovery_command

    payload = {
        "recovery": {"failed_node": "svc:redis-server", "failure_mode": "process_down",
                    "substrate": "systemd", "unit": "redis-server", "risk": 0.3,
                    "diagnosed_at": 100.0, "tenant": "acme", "dependents": []},
        "approval": {"approver": "alice", "tenant": "acme", "decision_goal": "recover:process_down",
                    "expires_at": 200.0, "action_id": "act-1",
                    "canonical_scope": "acme:svc:redis-server", "issued_at": 100.0,
                    "action_scope": "recover_service:svc:redis-server"},
        "evidence": {},
    }
    req, approval, ctx = decode_recovery_command(payload)
    assert req.unit == "redis-server"
