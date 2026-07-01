"""Tests: Trace Spine — event tương quan cho mọi transition runtime (Track B nền).

Mọi transition Track A (safety) phải quan sát được ở Track B. Spine này là read-model
MỎNG trên Redis (không source-of-truth thứ hai): emit event đủ correlation ID → console
đọc lại timeline E2E, list theo tenant, và mọi query cô lập theo tenant.
"""
from __future__ import annotations

import fakeredis.aioredis as aioredis
import pytest

from aoip.agent.trace import (
    EV_APPROVAL_VALIDATED,
    EV_COMMAND_RECEIVED,
    EV_COMPLETED,
    EV_MUTATION_STARTED,
    Correlation,
    RuntimeTrace,
    canonical_scope,
)

NOW = 1000.0


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _corr(*, tenant="acme", incident="inc-1", decision="dec-1", action="act-1",
          command="cmd-1", node="svc:cust-db"):
    return Correlation(
        tenant=tenant, agent_id="agent-x", mission_id="mis-1", incident_id=incident,
        decision_id=decision, action_id=action, command_id=command,
        canonical_scope=canonical_scope(tenant, node))


# ── canonical scope: tenant luôn nhúng ───────────────────────────────────────
def test_canonical_scope_embeds_tenant_and_normalizes():
    a = canonical_scope("acme", "svc:cust-db")
    b = canonical_scope("evil-corp", "svc:cust-db")
    assert a != b                       # cùng target, khác tenant → khác scope
    assert a.startswith("acme:")
    assert canonical_scope("acme", "SVC:Cust-DB") == a  # normalize case/space


# ── correlation_id: bất biến theo incident, không đổi giữa các event ──────────
def test_correlation_id_stable_per_incident():
    c1 = _corr(command="cmd-1")
    c2 = _corr(command="cmd-2")          # cùng incident, command khác
    assert c1.correlation_id == c2.correlation_id  # 1 incident = 1 timeline
    assert _corr(incident="inc-2").correlation_id != c1.correlation_id


# ── emit → timeline đọc lại theo thứ tự, tenant-isolated ──────────────────────
async def test_emit_builds_ordered_timeline():
    r = _redis()
    tr = RuntimeTrace(r)
    c = _corr()
    await tr.emit(EV_COMMAND_RECEIVED, c, state_before="", state_after="received",
                  reason="mission delivered", evidence_refs=(), ts=NOW)
    await tr.emit(EV_MUTATION_STARTED, c, state_before="approved", state_after="executing",
                  reason="restart redis-server", evidence_refs=("before:inactive",), ts=NOW + 2)
    tl = await tr.timeline(c.tenant, c.correlation_id)
    assert [e["event_type"] for e in tl] == [EV_COMMAND_RECEIVED, EV_MUTATION_STARTED]
    assert tl[0]["tenant_id"] == "acme" and tl[0]["command_id"] == "cmd-1"
    assert tl[1]["evidence_refs"] == ["before:inactive"]


async def test_every_event_carries_full_correlation_fields():
    r = _redis()
    c = _corr()
    await RuntimeTrace(r).emit(EV_COMPLETED, c, state_before="verifying",
                               state_after="completed", reason="verified", evidence_refs=(), ts=NOW)
    e = (await RuntimeTrace(r).timeline(c.tenant, c.correlation_id))[0]
    for k in ("tenant_id", "agent_id", "mission_id", "incident_id", "decision_id",
              "action_id", "command_id", "canonical_scope", "timestamp",
              "state_before", "state_after", "reason", "correlation_id"):
        assert k in e


# ── tenant isolation: query 1 tenant KHÔNG thấy tenant khác ───────────────────
async def test_tenant_isolation_on_lists():
    r = _redis()
    tr = RuntimeTrace(r)
    await tr.emit(EV_COMMAND_RECEIVED, _corr(tenant="acme"), state_before="",
                  state_after="received", reason="x", evidence_refs=(), ts=NOW)
    await tr.emit(EV_COMMAND_RECEIVED, _corr(tenant="globex", incident="inc-9"),
                  state_before="", state_after="received", reason="y", evidence_refs=(), ts=NOW)
    acme = await tr.list_timelines("acme")
    globex = await tr.list_timelines("globex")
    assert len(acme) == 1 and len(globex) == 1
    assert acme != globex


# ── pending approvals: index riêng, clear khi validated ──────────────────────
async def test_pending_approval_index_lifecycle():
    r = _redis()
    tr = RuntimeTrace(r)
    c = _corr()
    await tr.mark_pending_approval(c, reason="cần người duyệt restart", ts=NOW)
    assert len(await tr.pending_approvals("acme")) == 1
    await tr.emit(EV_APPROVAL_VALIDATED, c, state_before="pending", state_after="approved",
                  reason="alice approved", evidence_refs=(), ts=NOW + 5)
    await tr.clear_pending_approval(c)
    assert await tr.pending_approvals("acme") == []
