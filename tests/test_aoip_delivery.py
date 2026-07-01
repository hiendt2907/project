"""Tests: durable command delivery + agent inbox resume (Living Ops Runtime — Step 3).

Chứng minh máy trạng thái giao/runtime durable sống sót: GET=peek (fix P0), duplicate
delivery, agent crash, agent restart, Gateway outage, Redis/Gateway restart, report retry —
KHÔNG mất command, KHÔNG mutation lặp. Đây là proof ở tầng unit/integration; proof trên
K8s Gateway + VM thật ở scripts/deploy.
"""
from __future__ import annotations

import fakeredis.aioredis as aioredis
import pytest

from aoip.agent.delivery import (
    ST_ACCEPTED,
    ST_COMPLETED,
    ST_DELIVERED,
    ST_ESCALATED,
    ST_EXPIRED,
    ST_QUEUED,
    ST_RUNNING,
    CommandRecord,
    DurableCommandChannel,
)
from aoip.agent.inbox import (
    L_ACKED,
    L_OUTCOME_RECORDED,
    L_RECEIVED,
    L_RUNNING,
    LocalInbox,
)

NOW = 1000.0
TENANT = "acme"
AGENT = "agent-1"


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _rec(command_id="cmd-1", *, tenant=TENANT, agent=AGENT, incident="inc-1",
         expires_at=NOW + 300, payload_hash="ph-1"):
    return CommandRecord(
        command_id=command_id, tenant_id=tenant, agent_id=agent, mission_id="mis-1",
        incident_id=incident, decision_id="dec-1", action_id="act-1",
        canonical_scope=f"{tenant}:svc:db", payload_hash=payload_hash,
        payload={"verb": "restart", "unit": "redis-server"},
        created_at=NOW, expires_at=expires_at)


# ── P0 FIX: GET is PEEK, not POP ─────────────────────────────────────────────
async def test_get_is_peek_command_survives_fetch():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(), now=NOW)

    first = await ch.poll(TENANT, AGENT, now=NOW)
    assert [c.command_id for c in first] == ["cmd-1"]
    assert first[0].state == ST_DELIVERED and first[0].delivery_count == 1

    # Command KHÔNG biến mất — vẫn còn record; redelivery sau visibility timeout.
    rec = await ch.get(TENANT, "cmd-1")
    assert rec is not None and rec.state == ST_DELIVERED
    again = await ch.poll(TENANT, AGENT, now=NOW + 61)      # sau visibility 60s
    assert [c.command_id for c in again] == ["cmd-1"]
    assert again[0].delivery_count == 2                     # redelivery durable


async def test_delivered_command_not_redelivered_within_visibility():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(), now=NOW)
    await ch.poll(TENANT, AGENT, now=NOW)
    # trong cửa sổ visibility → KHÔNG giao lại (tránh double-processing song song)
    assert await ch.poll(TENANT, AGENT, now=NOW + 5) == []


# ── (7) expired command → zero mutation ──────────────────────────────────────
async def test_enqueue_expired_never_delivered():
    r = _redis()
    ch = DurableCommandChannel(r)
    rec = await ch.enqueue(_rec(expires_at=NOW - 1), now=NOW)
    assert rec.state == ST_EXPIRED
    assert await ch.poll(TENANT, AGENT, now=NOW) == []      # zero delivery


async def test_command_expiring_in_queue_is_expired_on_poll():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(expires_at=NOW + 10), now=NOW)
    assert await ch.poll(TENANT, AGENT, now=NOW + 20) == []  # hết hạn khi poll
    assert (await ch.get(TENANT, "cmd-1")).state == ST_EXPIRED


# ── (5) duplicate delivery mutates once (Gateway side) ───────────────────────
async def test_terminal_stops_redelivery_and_ack_idempotent():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(), now=NOW)
    await ch.poll(TENANT, AGENT, now=NOW)
    term = await ch.record_terminal(TENANT, "cmd-1", state=ST_COMPLETED,
                                    outcome={"rc": 0}, now=NOW + 1)
    assert term.state == ST_COMPLETED
    # terminal → không còn redelivery
    assert await ch.poll(TENANT, AGENT, now=NOW + 999) == []
    # report lại (redelivery muộn) → ack idempotent, outcome KHÔNG đổi
    again = await ch.record_terminal(TENANT, "cmd-1", state=ST_COMPLETED,
                                     outcome={"rc": 999}, now=NOW + 2)
    assert again.outcome == {"rc": 0} and again.terminal_at == NOW + 1


async def test_two_incidents_same_plan_separate_commands():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(command_id="cmd-a", incident="inc-1"), now=NOW)
    await ch.enqueue(_rec(command_id="cmd-b", incident="inc-2"), now=NOW)
    got = {c.command_id for c in await ch.poll(TENANT, AGENT, now=NOW, limit=10)}
    assert got == {"cmd-a", "cmd-b"}                        # tách biệt, không dedup nhầm


# ── (8-gateway) wrong tenant → isolated key, zero cross-tenant delivery ───────
async def test_tenant_isolation_no_cross_delivery():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(command_id="cmd-x", tenant="acme"), now=NOW)
    # agent cùng id nhưng tenant khác → không thấy command của acme
    assert await ch.poll("globex", AGENT, now=NOW) == []
    assert await ch.get("globex", "cmd-x") is None


# ── (6) Redis/Gateway restart không mất command state ────────────────────────
async def test_survives_gateway_restart_new_channel_same_redis():
    r = _redis()
    await DurableCommandChannel(r).enqueue(_rec(), now=NOW)
    await DurableCommandChannel(r).poll(TENANT, AGENT, now=NOW)   # "gateway restart"
    # instance kênh mới (giả lập Gateway process khác) đọc lại state từ Redis
    ch2 = DurableCommandChannel(r)
    rec = await ch2.get(TENANT, "cmd-1")
    assert rec is not None and rec.state == ST_DELIVERED
    assert "cmd-1" in await ch2.inflight(TENANT, AGENT)


# ── (failed verification) escalate, không redelivery vô hạn ──────────────────
async def test_escalated_is_terminal_no_infinite_retry():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(), now=NOW)
    await ch.poll(TENANT, AGENT, now=NOW)
    await ch.mark_progress(TENANT, "cmd-1", ST_RUNNING, now=NOW)
    esc = await ch.record_terminal(TENANT, "cmd-1", state=ST_ESCALATED,
                                   outcome={"reason": "verify_failed"}, now=NOW + 5)
    assert esc.state == ST_ESCALATED
    assert await ch.poll(TENANT, AGENT, now=NOW + 10_000) == []   # không retry vô hạn


async def test_ack_protocol_progression():
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(), now=NOW)
    await ch.poll(TENANT, AGENT, now=NOW)
    assert (await ch.mark_accepted(TENANT, "cmd-1", now=NOW)).state == ST_ACCEPTED
    assert (await ch.mark_progress(TENANT, "cmd-1", ST_RUNNING, now=NOW)).state == ST_RUNNING


# ══ Agent local inbox — crash/restart/report-retry proofs ════════════════════

# ── (1) crash before mutation resumes safely ─────────────────────────────────
def test_inbox_crash_before_mutation_resumes_via_reconcile(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={"verb": "restart"})
    box.set_state("cmd-1", L_RUNNING)                       # crash giữa lúc RUNNING
    # "restart agent" → inbox mới trên cùng thư mục
    resumed = LocalInbox(str(tmp_path)).pending()
    assert len(resumed) == 1
    entry = resumed[0]
    assert not entry.has_outcome and entry.needs_reconcile  # reconcile, KHÔNG blind retry


def test_inbox_crash_pre_running_safe_to_reprocess(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={})      # crash ngay sau RECEIVED
    entry = LocalInbox(str(tmp_path)).pending()[0]
    assert entry.local_state == L_RECEIVED and not entry.has_outcome
    assert not entry.needs_reconcile                        # chưa RUNNING → chưa mutation


# ── (2) crash after mutation before report → re-report, no re-mutation ────────
def test_inbox_outcome_recorded_reports_without_remutation(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={})
    box.set_state("cmd-1", L_RUNNING)
    box.record_outcome("cmd-1", {"rc": 0, "restarted": True})   # mutation xong, rồi crash
    entry = LocalInbox(str(tmp_path)).pending()[0]
    assert entry.local_state == L_OUTCOME_RECORDED
    assert entry.has_outcome and not entry.needs_reconcile      # chỉ RE-REPORT
    assert entry.outcome["restarted"] is True                   # outcome không mất


# ── (3) Gateway unavailable after execution → outcome preserved, report later ─
async def test_gateway_outage_then_report_later(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={})
    box.record_outcome("cmd-1", {"rc": 0})
    # Gateway down → không report được; outcome vẫn nằm cục bộ, đợi.
    pend = box.pending()
    assert pend[0].has_outcome
    # Gateway lên lại → report + Gateway ack → archive
    r = _redis()
    ch = DurableCommandChannel(r)
    await ch.enqueue(_rec(), now=NOW)
    await ch.poll(TENANT, AGENT, now=NOW)
    term = await ch.record_terminal(TENANT, "cmd-1", state=ST_COMPLETED,
                                    outcome=pend[0].outcome, now=NOW + 100)
    assert term is not None
    box.set_state("cmd-1", L_ACKED)
    box.archive("cmd-1")
    assert box.pending() == []                              # dọn sạch sau terminal ack


# ── (4-duplicate) redelivery: đã có outcome cục bộ → re-report, KHÔNG re-mutate ─
def test_inbox_duplicate_persist_keeps_outcome(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={})
    box.record_outcome("cmd-1", {"rc": 0})
    # redelivery cùng command_id → persist lại KHÔNG reset về RECEIVED
    again = box.persist("cmd-1", tenant_id=TENANT, payload={})
    assert again.local_state == L_OUTCOME_RECORDED and again.has_outcome


def test_inbox_atomic_write_no_partial_on_reopen(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={"k": "v"})
    # đọc lại từ đĩa (không dùng cache) → JSON hợp lệ, đủ field
    entry = LocalInbox(str(tmp_path)).get("cmd-1")
    assert entry is not None and entry.payload == {"k": "v"}
