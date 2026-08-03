"""Remote-host auto-execution: blast-radius gate, CRAT fail-closed, loop closure.

Covers the three things that were missing before 2026-08-02, when 0 of 809 remote
traces had ever reached the EXECUTOR stage:
  1. unattended dispatch is allowlisted per agent and fail-closed by default,
  2. no host mutation is dispatched or published without a CRAT block,
  3. a terminal durable-command outcome lands back on the originating trace.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from workers import auto_recovery_bridge as arb
from workers import remote_command_outcome_loop as rcol


# ── Test doubles ─────────────────────────────────────────────────────────────

class FakeRedis:
    """Minimal async Redis: strings + one ZSET. decode_responses=True semantics."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None, **kw):
        self.kv[key] = value
        return True

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        return [m for m, _ in items[start:end + 1 if end >= 0 else None]]

    async def zscore(self, key, member):
        return self.zsets.get(key, {}).get(member)

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0


class FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic, msg, key=None):
        self.sent.append((topic, msg))


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"state": "QUEUED"}

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        return self.response


def _settings():
    return SimpleNamespace(
        omni_gateway_api_key="test-key",
        omni_gateway_internal_url="http://gw.local",
        kafka_topic_audit_chain="omni-audit-chain",
        kafka_topic_action_feedback="omni-action-feedback",
    )


def _final(unit="payment-api", confidence=0.9):
    return {
        "root_cause": f"{unit} is down",
        "confidence": confidence,
        "suggested_recovery": {"capability": "systemd.restart_unit", "unit": unit},
    }


@pytest.fixture
def ctx():
    return SimpleNamespace(redis=FakeRedis(), kafka=FakeKafka(), settings=_settings())


@pytest.fixture
def audit_ok(monkeypatch):
    """Stub the CRAT writer so these tests exercise ordering, not hash-chaining."""
    written: list[dict] = []

    async def _write(**kwargs):
        written.append(kwargs)
        return {"block_hash": "deadbeef"}

    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", _write)
    return written


# ── 1. Blast-radius allowlist ────────────────────────────────────────────────

def test_lab_allowlist_is_empty_when_env_unset():
    assert arb.lab_auto_execute_agents({}) == frozenset()


def test_lab_allowlist_is_empty_when_env_blank():
    assert arb.lab_auto_execute_agents({"OMNI_LAB_AUTO_EXECUTE_AGENTS": "   "}) == frozenset()


def test_lab_allowlist_parses_and_trims_comma_list():
    env = {"OMNI_LAB_AUTO_EXECUTE_AGENTS": "a_one, b_two ,,c_three"}
    assert arb.lab_auto_execute_agents(env) == frozenset({"a_one", "b_two", "c_three"})


async def test_dispatch_blocked_when_agent_not_allowlisted(ctx, monkeypatch, audit_ok):
    monkeypatch.delenv("OMNI_LAB_AUTO_EXECUTE_AGENTS", raising=False)
    http = FakeHttp()
    result = await arb.dispatch_if_eligible(
        settings=ctx.settings, http_client=http, final=_final(),
        agent_id="prod_agent", tenant_id="t1", trace_id="tr-1",
        redis=ctx.redis, kafka=ctx.kafka,
    )
    assert result["dispatched"] is False
    assert result["reason"] == "agent_not_in_lab_allowlist"
    assert http.calls == [], "no HTTP call may be made for a non-allowlisted agent"
    assert audit_ok == [], "a blocked dispatch must not write an audit block"


async def test_dispatch_proceeds_for_allowlisted_agent(ctx, monkeypatch, audit_ok):
    monkeypatch.setenv("OMNI_LAB_AUTO_EXECUTE_AGENTS", "lab_agent")
    http = FakeHttp()
    result = await arb.dispatch_if_eligible(
        settings=ctx.settings, http_client=http, final=_final(),
        agent_id="lab_agent", tenant_id="t1", trace_id="tr-1",
        redis=ctx.redis, kafka=ctx.kafka,
    )
    assert result["dispatched"] is True
    assert len(http.calls) == 1
    assert http.calls[0][0].endswith("/webhook/agent/rt/commands/enqueue")


# ── 2. CRAT fail-closed ──────────────────────────────────────────────────────

async def test_dispatch_refused_without_audit_transport(ctx, monkeypatch):
    """redis/kafka absent => cannot audit => must not dispatch."""
    monkeypatch.setenv("OMNI_LAB_AUTO_EXECUTE_AGENTS", "lab_agent")
    http = FakeHttp()
    result = await arb.dispatch_if_eligible(
        settings=ctx.settings, http_client=http, final=_final(),
        agent_id="lab_agent", tenant_id="t1", trace_id="tr-1",
    )
    assert result["dispatched"] is False
    assert result["reason"] == "audit_ledger_unavailable"
    assert http.calls == []


async def test_audit_block_written_before_http_dispatch(ctx, monkeypatch):
    """The ledger write must happen first — an unaudited mutation must be impossible."""
    monkeypatch.setenv("OMNI_LAB_AUTO_EXECUTE_AGENTS", "lab_agent")
    order: list[str] = []

    async def _write(**kwargs):
        order.append("audit")
        return {"block_hash": "x"}

    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", _write)

    class OrderedHttp(FakeHttp):
        async def post(self, url, json=None, headers=None, timeout=None):
            order.append("http")
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    await arb.dispatch_if_eligible(
        settings=ctx.settings, http_client=OrderedHttp(), final=_final(),
        agent_id="lab_agent", tenant_id="t1", trace_id="tr-1",
        redis=ctx.redis, kafka=ctx.kafka,
    )
    assert order == ["audit", "http"]


async def test_dispatch_aborted_when_audit_write_fails(ctx, monkeypatch):
    monkeypatch.setenv("OMNI_LAB_AUTO_EXECUTE_AGENTS", "lab_agent")
    from services.audit_ledger.chain_writer import AuditLedgerError

    async def _boom(**kwargs):
        raise AuditLedgerError("redis chain down")

    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", _boom)
    http = FakeHttp()
    result = await arb.dispatch_if_eligible(
        settings=ctx.settings, http_client=http, final=_final(),
        agent_id="lab_agent", tenant_id="t1", trace_id="tr-1",
        redis=ctx.redis, kafka=ctx.kafka,
    )
    assert result["dispatched"] is False
    assert result["reason"].startswith("crat_write_failed")
    assert http.calls == [], "FAIL_CLOSED: no dispatch after a failed audit write"


async def test_successful_dispatch_registers_pending_entry(ctx, monkeypatch, audit_ok):
    monkeypatch.setenv("OMNI_LAB_AUTO_EXECUTE_AGENTS", "lab_agent")
    result = await arb.dispatch_if_eligible(
        settings=ctx.settings, http_client=FakeHttp(), final=_final(),
        agent_id="lab_agent", tenant_id="t1", trace_id="tr-1",
        redis=ctx.redis, kafka=ctx.kafka,
    )
    member = arb.pending_member("t1", result["command_id"])
    assert member in ctx.redis.zsets[arb.PENDING_KEY]
    meta = json.loads(ctx.redis.kv[f"omni:autorecovery:meta:t1:{result['command_id']}"])
    assert meta["trace_id"] == "tr-1"
    assert meta["unit"] == "payment-api.service"


# ── 3. Outcome loop closes the trace ─────────────────────────────────────────

def _seed(ctx, *, state="COMPLETED", rc=0, tenant="t1", cid="cmd-1", trace="tr-1"):
    ctx.redis.kv[f"omni:cmd:rec:{tenant}:{cid}"] = json.dumps({
        "command_id": cid, "agent_id": "lab_agent", "state": state,
        "delivery_attempt": 1, "action_id": "act-1", "canonical_scope": f"{tenant}:svc:x",
        "incident_id": trace,
        "outcome": {"status": "recovered" if rc == 0 else "aborted", "rc": rc,
                    "reason": "service + dependents verified",
                    "evidence": ["before=inactive", "service_health=ok"],
                    "verified": rc == 0},
    })
    ctx.redis.kv[f"omni:autorecovery:meta:{tenant}:{cid}"] = json.dumps({
        "trace_id": trace, "agent_id": "lab_agent", "unit": "payment-api.service",
        "capability": "systemd.restart_unit",
    })
    # Score is the dispatch time; use "now" so the stale-abandon path does not fire.
    ctx.redis.zsets.setdefault(arb.PENDING_KEY, {})[arb.pending_member(tenant, cid)] = time.time()


def test_split_member_rejects_malformed():
    assert rcol.split_member("t1|cmd-1") == ("t1", "cmd-1")
    assert rcol.split_member("no-separator") is None
    assert rcol.split_member("|cmd-1") is None
    assert rcol.split_member("t1|") is None


def test_outcome_exit_code_falls_back_to_state():
    assert rcol.outcome_exit_code({"state": "COMPLETED", "outcome": {}}) == 0
    assert rcol.outcome_exit_code({"state": "EXPIRED", "outcome": {}}) == 1
    assert rcol.outcome_exit_code({"state": "COMPLETED", "outcome": {"rc": 3}}) == 3


async def test_non_terminal_command_stays_pending(ctx, audit_ok):
    _seed(ctx, state="RUNNING")
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "pending"
    assert arb.pending_member("t1", "cmd-1") in ctx.redis.zsets[arb.PENDING_KEY]


async def test_completed_command_marks_executor_and_feedback(ctx, audit_ok):
    _seed(ctx)
    marked: list[tuple[str, str, str]] = []

    async def _mark(redis, trace_id, stage, status="ok", *, detail="", lane=""):
        marked.append((trace_id, stage, status))

    import workers.remote_command_outcome_loop as mod
    original, mod.mark_stage = mod.mark_stage, _mark
    try:
        assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"
    finally:
        mod.mark_stage = original

    assert ("tr-1", "EXECUTOR", "ok") in marked
    assert ("tr-1", "FEEDBACK", "ok") in marked
    topics = [t for t, _ in ctx.kafka.sent]
    assert "omni-action-feedback" in topics


async def test_failed_command_marks_stages_as_fail(ctx, audit_ok):
    _seed(ctx, state="FAILED", rc=1)
    marked: list[tuple[str, str, str]] = []

    async def _mark(redis, trace_id, stage, status="ok", *, detail="", lane=""):
        marked.append((trace_id, stage, status))

    import workers.remote_command_outcome_loop as mod
    original, mod.mark_stage = mod.mark_stage, _mark
    try:
        assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"
    finally:
        mod.mark_stage = original

    assert ("tr-1", "EXECUTOR", "fail") in marked
    assert ("tr-1", "FEEDBACK", "fail") in marked


async def test_outcome_not_published_when_audit_fails(ctx, monkeypatch):
    _seed(ctx)

    async def _boom(**kwargs):
        raise RuntimeError("chain down")

    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", _boom)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "retry"
    assert ctx.kafka.sent == [], "FAIL_CLOSED: no feedback published without an audit block"


async def test_missing_record_is_abandoned(ctx, audit_ok):
    assert await rcol.reconcile_one(ctx, "t1", "gone") == "abandoned"


async def test_drain_removes_completed_and_keeps_pending(ctx, audit_ok):
    _seed(ctx, cid="cmd-done", trace="tr-done")
    _seed(ctx, cid="cmd-run", trace="tr-run", state="RUNNING")
    tally = await rcol.drain_once(ctx)
    assert tally["done"] == 1
    assert tally["pending"] == 1
    remaining = ctx.redis.zsets[arb.PENDING_KEY]
    assert arb.pending_member("t1", "cmd-done") not in remaining
    assert arb.pending_member("t1", "cmd-run") in remaining


async def test_stale_never_terminal_entry_is_abandoned(ctx, audit_ok):
    """A command stuck non-terminal past the gateway's own record TTL has nothing
    left to reconcile against and must not be retried forever."""
    _seed(ctx, cid="cmd-old", trace="tr-old", state="RUNNING")
    ctx.redis.zsets[arb.PENDING_KEY][arb.pending_member("t1", "cmd-old")] = (
        time.time() - rcol._ABANDON_AFTER_S - 1
    )
    tally = await rcol.drain_once(ctx)
    assert tally["abandoned"] == 1
    assert arb.pending_member("t1", "cmd-old") not in ctx.redis.zsets[arb.PENDING_KEY]


async def test_reconciled_trace_is_flagged_terminal(ctx, audit_ok):
    """Prevents the K8s re-planner from trying to follow up a VM systemd recovery."""
    _seed(ctx)
    await rcol.reconcile_one(ctx, "t1", "cmd-1")
    assert ctx.redis.kv.get("omni:autonomous:terminal:tr-1") == "remote_recovery_terminal"


# ── 4. Agent may never be ordered to restart itself ──────────────────────────

def test_self_unit_names_cover_both_naming_conventions():
    from aoip.agent.runtime_config import self_unit_names

    names = self_unit_names({"OMNI_AGENT_SYSTEMD_SERVICE": "aoip-agent"})
    assert "aoip-agent.service" in names
    assert "omni-remote-agent.service" in names


def test_agent_own_unit_stripped_from_gate_allowlist():
    from aoip.agent.runtime_config import _build_gate

    env = {
        "AOIP_GATE_ALLOWED_FAILURE_MODES": "process_down",
        "AOIP_GATE_ALLOWED_SUBSTRATES": "systemd",
        "AOIP_GATE_SCOPE_PREFIX": "svc:",
        "AOIP_GATE_MAX_RISK": "0.5",
        "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE": "0.5",
        "AOIP_GATE_MAX_DIAGNOSIS_AGE_S": "300",
        "AOIP_ALLOWED_SYSTEMD_UNITS": "payment-api.service,aoip-agent.service",
    }
    gate = _build_gate(env)
    assert "payment-api.service" in gate.allowed_targets
    assert "aoip-agent.service" not in gate.allowed_targets, (
        "self-restart is an observability-loss loop and must be stripped"
    )


def test_self_restart_possible_only_with_explicit_override():
    from aoip.agent.runtime_config import _build_gate

    env = {
        "AOIP_GATE_ALLOWED_FAILURE_MODES": "process_down",
        "AOIP_GATE_ALLOWED_SUBSTRATES": "systemd",
        "AOIP_GATE_SCOPE_PREFIX": "svc:",
        "AOIP_GATE_MAX_RISK": "0.5",
        "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE": "0.5",
        "AOIP_GATE_MAX_DIAGNOSIS_AGE_S": "300",
        "AOIP_ALLOWED_SYSTEMD_UNITS": "aoip-agent.service",
        "AOIP_ALLOW_SELF_RESTART": "true",
    }
    assert "aoip-agent.service" in _build_gate(env).allowed_targets
