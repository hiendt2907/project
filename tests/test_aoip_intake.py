"""Tests: Slice 1A — intake + fail-closed gates + observable timeline.

Chứng minh: duplicate / expired / wrong-tenant / wrong-scope / wrong-decision → ZERO
mutation (admitted=False) VÀ mỗi quyết định hiện ra ở timeline (Track B). Hai incident
cùng plan KHÔNG collide idempotency (#4). Cùng target 2 tenant KHÔNG chặn nhau (#5).
"""
from __future__ import annotations

import fakeredis.aioredis as aioredis
import pytest

from aoip.agent.intake import _idem_key, admit_command
from aoip.agent.trace import (
    EV_ABORTED,
    EV_APPROVAL_REJECTED,
    EV_APPROVAL_VALIDATED,
    EV_COMMAND_RECEIVED,
    EV_IDEMPOTENCY_CLAIMED,
    EV_LEASE_ACQUIRED,
    Correlation,
    RuntimeTrace,
    canonical_scope,
)
from aoip.objects import ActionState
from aoip.recovery import Approval, RecoveryGate, RecoveryRequest, plan_recovery

NOW = 1000.0


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0)


def _req(tenant="acme", node="svc:db"):
    action = plan_recovery(failed_node=node, failure_mode="process_down", substrate="systemd",
                           unit="redis-server", port=6379, risk=0.3).at(ActionState.APPROVED)
    return RecoveryRequest(failed_node=node, failure_mode="process_down", substrate="systemd",
                           unit="redis-server", port=6379, action=action, risk=0.3,
                           diagnosed_at=NOW, dependents=(), tenant=tenant)


def _corr(*, tenant="acme", incident="inc-1", node="svc:db", command="cmd-1", action_id="act-1"):
    return Correlation(tenant=tenant, agent_id="agent-1", mission_id="mis-1", incident_id=incident,
                       decision_id="dec-1", action_id=action_id, command_id=command,
                       canonical_scope=canonical_scope(tenant, node))


def _approval(corr, req, *, tenant=None, canonical=None, decision=None, action_id=None,
              issued_at=NOW - 10, expires_at=NOW + 300):
    return Approval.issue(
        approver="alice", tenant=tenant or corr.tenant,
        canonical_scope=canonical or corr.canonical_scope,
        decision_goal=decision or req.action.decision_goal,
        action_id=action_id or corr.action_id, action_scope=req.action.scope,
        issued_at=issued_at, expires_at=expires_at)


async def _admit(r, corr, req, appr, *, holder="agent-1"):
    return await admit_command(corr=corr, req=req, gate=_gate(), approval=appr, now=NOW,
                               redis=r, holder=holder, trace=RuntimeTrace(r))


async def _events(r, corr):
    return [e["event_type"] for e in await RuntimeTrace(r).timeline(corr.tenant, corr.correlation_id)]


# ── happy path: admitted + timeline đầy đủ ───────────────────────────────────
async def test_admit_happy_path_observable():
    r = _redis()
    corr, req = _corr(), _req()
    adm = await _admit(r, corr, req, _approval(_corr(), _req()))
    assert adm.admitted and adm.lease_token and adm.idem_key
    assert await _events(r, corr) == [EV_COMMAND_RECEIVED, EV_IDEMPOTENCY_CLAIMED,
                                      EV_LEASE_ACQUIRED, EV_APPROVAL_VALIDATED]


# ── seq đơn điệu để phát hiện thiếu/đảo ──────────────────────────────────────
async def test_events_carry_monotonic_seq():
    r = _redis()
    corr, req = _corr(), _req()
    await _admit(r, corr, req, _approval(_corr(), _req()))
    tl = await RuntimeTrace(r).timeline(corr.tenant, corr.correlation_id)
    assert [e["seq"] for e in tl] == [1, 2, 3, 4]
    assert all(e["source_version"] is not None for e in tl)


# ── (2) duplicate delivery → zero mutation, hiện ABORTED ─────────────────────
async def test_duplicate_delivery_aborts_and_observable():
    r = _redis()
    corr, req, appr = _corr(), _req(), _approval(_corr(), _req())
    a1 = await _admit(r, corr, req, appr)
    a2 = await _admit(r, corr, req, appr)   # giao trùng y hệt
    assert a1.admitted and not a2.admitted
    assert "duplicate" in a2.reason
    assert EV_ABORTED in await _events(r, corr)


# ── (expired / wrong-tenant / wrong-scope / wrong-decision) → REJECTED ────────
@pytest.mark.parametrize("kw", [
    {"expires_at": NOW - 1},
    {"tenant": "evil-corp"},
    {"canonical": "acme:svc:other"},
    {"decision": "recover:disk_full"},
    {"action_id": "act-WRONG"},
])
async def test_bad_approval_rejected_zero_mutation(kw):
    r = _redis()
    corr, req = _corr(), _req()
    appr = _approval(_corr(), _req(), **kw)
    adm = await _admit(r, corr, req, appr)
    assert not adm.admitted
    evs = await _events(r, corr)
    assert EV_APPROVAL_REJECTED in evs and EV_ABORTED in evs


# ── rejected → lease + claim được nhả để lần hợp lệ sau vẫn đi được ──────────
async def test_reject_releases_lease_and_claim():
    r = _redis()
    corr, req = _corr(), _req()
    bad = await _admit(r, corr, req, _approval(_corr(), _req(), tenant="evil-corp"))
    assert not bad.admitted
    good = await _admit(r, corr, req, _approval(_corr(), _req()))  # cùng command, approval đúng
    assert good.admitted


# ── (#4) hai incident cùng plan → KHÔNG collide idempotency ──────────────────
async def test_two_incidents_same_plan_distinct_identity():
    r = _redis()
    c1, c2 = _corr(incident="inc-1", command="cmd-1"), _corr(incident="inc-2", command="cmd-2")
    req = _req()
    assert _idem_key(c1, req) != _idem_key(c2, req)     # khác identity → không dedup nhầm
    a1 = await _admit(r, c1, req, _approval(_corr(incident="inc-1"), _req()))
    # incident 2 cùng target → bị LEASE serialize (không phải bị dedup)
    a2 = await _admit(r, c2, req, _approval(_corr(incident="inc-2"), _req()), holder="agent-2")
    assert a1.admitted
    assert not a2.admitted and "lease" in a2.reason.lower()  # serialize, KHÔNG "duplicate"


# ── (#5) cùng target name ở 2 tenant → không chặn nhau ───────────────────────
async def test_same_target_two_tenants_isolated():
    r = _redis()
    ca = _corr(tenant="acme", node="svc:db")
    cb = _corr(tenant="globex", node="svc:db", incident="inc-1")
    ra, rb = _req(tenant="acme"), _req(tenant="globex")
    a = await _admit(r, ca, ra, _approval(_corr(tenant="acme"), _req(tenant="acme")))
    b = await _admit(r, cb, rb, _approval(_corr(tenant="globex"), _req(tenant="globex")), holder="agent-2")
    assert a.admitted and b.admitted            # canonical scope tenant-embedded → lease riêng


# ── strict Approval.issue fail-closed: thiếu binding → raise ─────────────────
def test_bounded_approval_requires_all_bindings():
    with pytest.raises(ValueError):
        Approval.issue(approver="", tenant="acme", canonical_scope="acme:svc:db",
                       decision_goal="recover:process_down", action_id="a", action_scope="s",
                       issued_at=NOW, expires_at=NOW + 1)
    with pytest.raises(ValueError):  # expiry vô cực không được phép production
        Approval.issue(approver="alice", tenant="acme", canonical_scope="acme:svc:db",
                       decision_goal="recover:process_down", action_id="a", action_scope="s",
                       issued_at=NOW, expires_at=float("inf"))
