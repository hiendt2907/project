"""Step 0 — Admin config store (PostgreSQL omni_admin) + Transactional Outbox drainer.

No mocks: fakeredis.aioredis cho cache + CRAT chain; FakePgPool in-memory mô phỏng
đúng các query asyncpg repo/drainer phát ra (snapshot/rollback trên transaction để
test fail-closed). Ref: docs/MASTER_PLAN_autonomy_tiers.md §6.5, §8 step 0.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import fakeredis.aioredis
import pytest

from services.admin_config.drainer import CratOutboxDrainer
from services.admin_config.repo import AdminConfigRepo


# ── Fake Kafka (send_dict contract) ───────────────────────────────────────────
class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bytes | None]] = []

    async def send_dict(self, topic: str, message: dict[str, Any], key: bytes | None = None) -> None:
        self.sent.append((topic, message, key))


# ── Fake asyncpg pool (in-memory, query-substring routed) ─────────────────────
class _Row(dict):
    """Hỗ trợ row["col"] như asyncpg.Record."""


class _FakeConn:
    def __init__(self, store: "_Store") -> None:
        self._s = store

    # transaction: snapshot → restore khi có exception (mô phỏng rollback Postgres)
    def transaction(self) -> "_FakeTx":
        return _FakeTx(self._s)

    async def fetchrow(self, sql: str, *args: Any) -> _Row | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        s = self._s
        if "SELECT 1 FROM omni_admin.tenant WHERE tenant_id" in sql:
            return 1 if args[0] in s.tenant else None
        if "INSERT INTO omni_admin.tenant_api_key" in sql and "RETURNING id" in sql:
            tenant, key_hash, key_prefix, label, created_by = args
            kid = s.next_id()
            s.api_key[kid] = {
                "id": kid, "tenant_id": tenant, "key_hash": key_hash,
                "key_prefix": key_prefix, "label": label, "status": "active",
                "created_by": created_by, "created_at": None, "revoked_at": None,
            }
            return kid
        raise AssertionError(f"FakeConn.fetchval chưa hỗ trợ SQL: {sql[:80]}")

    async def fetch(self, sql: str, *args: Any) -> list[_Row]:
        s = self._s
        # ── list reads (Admin UI) ──────────────────────────────────────────
        if "FROM omni_admin.runtime_flag WHERE tenant_id = $1 ORDER BY" in sql:
            return [
                _Row({"flag_key": k[1], "updated_by": "op", "updated_at": None, **v})
                for k, v in s.runtime_flag.items() if k[0] == args[0]
            ]
        if "FROM omni_admin.risk_class_override WHERE tenant_id = $1" in sql and "tool_name = $2" not in sql:
            return [
                _Row({"tool_name": k[1], "reason": None, "updated_by": "op",
                      "updated_at": None, **v})
                for k, v in s.risk_override.items() if k[0] == args[0]
            ]
        if "FROM omni_admin.tenant t" in sql:
            return [
                _Row({**t, "created_at": None,
                      "active_keys": sum(1 for kk in s.api_key.values()
                                         if kk["tenant_id"] == t["tenant_id"] and kk["status"] == "active")})
                for t in s.tenant.values()
            ]
        if "FROM omni_admin.tenant_api_key WHERE tenant_id = $1 ORDER BY" in sql:
            return [_Row(k) for k in s.api_key.values() if k["tenant_id"] == args[0]]
        if "FROM omni_admin.hitl_decision" in sql and "decision = 'PENDING'" in sql:
            return [_Row(h) for h in s.hitl.values()
                    if h["tenant_id"] == args[0] and h["decision"] == "PENDING"]
        if "FROM omni_admin.hitl_decision WHERE pending_id = $1" in sql:
            h = s.hitl.get(args[0])
            return [_Row(h)] if h and h["tenant_id"] == args[1] else []
        if "FROM omni_admin.tenant_api_key WHERE id = $1" in sql:
            k = s.api_key.get(args[0])
            return [_Row(k)] if k else []
        if "FROM omni_admin.autonomy_tier_state" in sql and "SELECT" in sql:
            t = s.tier_state.get(args[0])
            return [_Row(t)] if t else []
        if "FROM omni_admin.runtime_flag" in sql and "SELECT" in sql:
            f = s.runtime_flag.get((args[0], args[1]))
            return [_Row(f)] if f else []
        if "FROM omni_admin.risk_class_override" in sql and "SELECT" in sql:
            r = s.risk_override.get((args[0], args[1]))
            return [_Row(r)] if r else []
        if "count(*)" in sql and "crat_outbox" in sql:
            n = sum(1 for o in s.crat_outbox if o["status"] == "PENDING")
            return [_Row(n=n)]
        if "FROM omni_admin.crat_outbox" in sql and "PENDING" in sql:
            limit = args[0] if args else len(s.crat_outbox)
            pend = [o for o in s.crat_outbox if o["status"] == "PENDING"]
            pend.sort(key=lambda o: o["id"])
            return [_Row(o) for o in pend[:limit]]
        raise AssertionError(f"FakeConn.fetch chưa hỗ trợ SQL: {sql[:80]}")

    async def execute(self, sql: str, *args: Any) -> str:
        s = self._s
        if "INSERT INTO omni_admin.autonomy_tier_state" in sql:
            tenant, tier, actor, version = args
            s.tier_state[tenant] = {"tenant_id": tenant, "tier": tier, "version": version}
            return "INSERT 0 1"
        if "INSERT INTO omni_admin.autonomy_tier_history" in sql:
            s.tier_history.append(args)
            return "INSERT 0 1"
        if "INSERT INTO omni_admin.runtime_flag" in sql:
            tenant, key, val_json, vtype, actor, version = args
            s.runtime_flag[(tenant, key)] = {
                "flag_value": val_json, "value_type": vtype, "version": version,
            }
            return "INSERT 0 1"
        if "INSERT INTO omni_admin.risk_class_override" in sql:
            tenant, tool, rc, reason, actor, version = args
            s.risk_override[(tenant, tool)] = {"risk_class": rc, "version": version}
            return "INSERT 0 1"
        if "INSERT INTO omni_admin.config_change_log" in sql:
            s.config_change_log.append(args)
            return "INSERT 0 1"
        if "INSERT INTO omni_admin.crat_outbox" in sql:
            dedup_key, event_type, payload_json = args
            if any(o["dedup_key"] == dedup_key for o in s.crat_outbox):
                return "INSERT 0 0"  # ON CONFLICT DO NOTHING — idempotent
            s.crat_outbox.append({
                "id": s.next_id(), "dedup_key": dedup_key, "event_type": event_type,
                "payload": payload_json, "status": "PENDING", "attempts": 0,
                "crat_ref": None, "last_error": None,
            })
            return "INSERT 0 1"
        if "UPDATE omni_admin.crat_outbox SET status='SENT'" in sql:
            rid, crat_ref = args
            for o in s.crat_outbox:
                if o["id"] == rid:
                    o.update(status="SENT", crat_ref=crat_ref, attempts=o["attempts"] + 1)
            return "UPDATE 1"
        if "UPDATE omni_admin.crat_outbox SET status=$2" in sql:
            rid, status, attempts, last_error = args
            for o in s.crat_outbox:
                if o["id"] == rid:
                    o.update(status=status, attempts=attempts, last_error=last_error)
            return "UPDATE 1"
        if "INSERT INTO omni_admin.tenant " in sql:
            tenant, display_name = args
            s.tenant[tenant] = {"tenant_id": tenant, "display_name": display_name, "status": "active"}
            return "INSERT 0 1"
        if "UPDATE omni_admin.tenant SET status=$2" in sql:
            tenant, status = args
            s.tenant[tenant]["status"] = status
            return "UPDATE 1"
        if "UPDATE omni_admin.tenant_api_key SET status='revoked'" in sql:
            s.api_key[args[0]].update(status="revoked")
            return "UPDATE 1"
        if "UPDATE omni_admin.hitl_decision SET decision=$2" in sql:
            pid, decision, actor, channel = args
            s.hitl[pid].update(decision=decision, actor=actor, channel=channel)
            return "UPDATE 1"
        if sql.strip().startswith("RAISE"):
            raise RuntimeError("injected postgres failure")
        raise AssertionError(f"FakeConn.execute chưa hỗ trợ SQL: {sql[:80]}")


class _FakeTx:
    def __init__(self, store: "_Store") -> None:
        self._s = store

    async def __aenter__(self) -> None:
        self._snap = self._s.snapshot()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._s.restore(self._snap)  # rollback
        return False


class _Acquire:
    def __init__(self, store: "_Store") -> None:
        self._s = store

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._s)

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _Store:
    def __init__(self) -> None:
        self.tier_state: dict[str, dict] = {}
        self.tier_history: list = []
        self.runtime_flag: dict[tuple, dict] = {}
        self.risk_override: dict[tuple, dict] = {}
        self.config_change_log: list = []
        self.crat_outbox: list[dict] = []
        self.tenant: dict[str, dict] = {}
        self.api_key: dict[int, dict] = {}
        self.hitl: dict[str, dict] = {}
        self._id = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def snapshot(self) -> dict:
        return copy.deepcopy(self.__dict__)

    def restore(self, snap: dict) -> None:
        self.__dict__.update(copy.deepcopy(snap))


class FakePgPool:
    def __init__(self) -> None:
        self.store = _Store()

    def acquire(self) -> _Acquire:
        return _Acquire(self.store)


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def pool() -> FakePgPool:
    return FakePgPool()


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class _Settings:
    crat_outbox_poll_interval_sec = 0.01
    crat_outbox_batch_size = 32
    crat_outbox_max_attempts = 3


# ── resolve_agent_thresholds: runtime-flag → agent warn thresholds ───────────
async def test_resolve_agent_thresholds_defaults_on_miss(redis):
    from services.admin_config.agent_thresholds import resolve_agent_thresholds

    out = await resolve_agent_thresholds(redis, "default")
    assert out == {"cpu_warn": 80.0, "mem_warn": 85.0, "disk_warn": 90.0}


async def test_resolve_agent_thresholds_reads_cache(redis):
    from services.admin_config.agent_thresholds import resolve_agent_thresholds
    from services.admin_config.cache import cache_key_runtime_flag

    await redis.set(cache_key_runtime_flag("default", "agent.cpu_warn"), "65")
    await redis.set(cache_key_runtime_flag("default", "agent.mem_warn"), "70")
    out = await resolve_agent_thresholds(redis, "default")
    assert out["cpu_warn"] == 65.0
    assert out["mem_warn"] == 70.0
    assert out["disk_warn"] == 90.0  # unset → default


async def test_resolve_agent_thresholds_rejects_out_of_range(redis):
    from services.admin_config.agent_thresholds import resolve_agent_thresholds
    from services.admin_config.cache import cache_key_runtime_flag

    await redis.set(cache_key_runtime_flag("default", "agent.cpu_warn"), "999")
    await redis.set(cache_key_runtime_flag("default", "agent.mem_warn"), "garbage")
    out = await resolve_agent_thresholds(redis, "default")
    assert out["cpu_warn"] == 80.0  # >100 rejected
    assert out["mem_warn"] == 85.0  # unparseable rejected


# ── set_tier: atomic 3-in-1 TX + write-through cache ─────────────────────────
async def test_set_tier_writes_state_history_log_and_outbox(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    out = await repo.set_tier(tier="assist", actor="op:alice", readiness={"wilson_lb": 0.82})

    assert out["tier"] == "assist"
    assert out["version"] == 1
    assert pool.store.tier_state["default"]["tier"] == "assist"
    assert len(pool.store.tier_history) == 1
    assert len(pool.store.config_change_log) == 1
    assert len(pool.store.crat_outbox) == 1
    ob = pool.store.crat_outbox[0]
    assert ob["status"] == "PENDING"
    assert ob["event_type"] == "AUTONOMY_TIER_CHANGED"
    # write-through cache
    assert await redis.get("omni:cfg:tier:default") == "assist"


async def test_set_tier_version_increments(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_tier(tier="assist", actor="op")
    out2 = await repo.set_tier(tier="auto", actor="op")
    assert out2["version"] == 2
    assert out2["dedup_key"] == "tier:default:2"


async def test_set_tier_rejects_invalid(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    with pytest.raises(ValueError):
        await repo.set_tier(tier="god-mode", actor="op")


# ── get_tier: cache → Postgres ───────────────────────────────────────────────
async def test_get_tier_reads_cache_then_pg(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    assert await repo.get_tier() is None  # chưa có
    pool.store.tier_state["default"] = {"tenant_id": "default", "tier": "auto", "version": 1}
    assert await repo.get_tier() == "auto"  # từ Postgres
    assert await redis.get("omni:cfg:tier:default") == "auto"  # đã nạp cache


# ── runtime_flag + risk_class ────────────────────────────────────────────────
async def test_set_runtime_flag_roundtrip(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_runtime_flag(
        flag_key="omni_hitl_escalation_timeout_sec", flag_value=600,
        value_type="int", actor="op",
    )
    assert await repo.get_runtime_flag("omni_hitl_escalation_timeout_sec") == 600
    assert pool.store.crat_outbox[0]["event_type"] == "CONFIG_CHANGED"


async def test_set_risk_class_override_roundtrip(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_risk_class_override(tool_name="k8s_scale_resource", risk_class="HIGH", actor="op")
    assert await repo.get_risk_class_override("k8s_scale_resource") == "HIGH"


async def test_set_risk_class_rejects_invalid(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    with pytest.raises(ValueError):
        await repo.set_risk_class_override(tool_name="x", risk_class="LOLNO", actor="op")


# ── fail-closed: Postgres fail giữa TX → rollback, Redis không lệch ──────────
async def test_pg_failure_aborts_tx_and_leaves_cache_untouched(pool, redis, monkeypatch):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_tier(tier="assist", actor="op")
    await redis.delete("omni:cfg:tier:default")  # giả lập cache miss trước lần ghi lỗi

    # Ép execute INSERT history ném lỗi → TX rollback
    orig = _FakeConn.execute
    async def boom(self, sql, *args):
        if "autonomy_tier_history" in sql:
            raise RuntimeError("injected postgres failure")
        return await orig(self, sql, *args)
    monkeypatch.setattr(_FakeConn, "execute", boom)

    with pytest.raises(RuntimeError):
        await repo.set_tier(tier="auto", actor="op")

    # state rollback về assist (v1), outbox không thêm, cache không bị set 'auto'
    assert pool.store.tier_state["default"]["tier"] == "assist"
    assert len(pool.store.crat_outbox) == 1
    assert await redis.get("omni:cfg:tier:default") is None


# ── drainer: enqueue → SENT, idempotent, fail retry ──────────────────────────
async def test_drainer_writes_crat_block_and_marks_sent(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_tier(tier="assist", actor="op")
    kafka = _FakeKafka()
    drainer = CratOutboxDrainer(pool, redis=redis, kafka=kafka, settings=_Settings())

    sent = await drainer.drain_once()
    assert sent == 1
    ob = pool.store.crat_outbox[0]
    assert ob["status"] == "SENT"
    assert ob["crat_ref"]  # block_hash
    # CRAT chain thực sự được ghi vào Redis
    assert await redis.llen("audit_chain:blocks") == 1
    assert len(kafka.sent) == 1
    assert await drainer.pending_count() == 0


async def test_drainer_idempotent_no_double_block(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_tier(tier="assist", actor="op")
    kafka = _FakeKafka()
    drainer = CratOutboxDrainer(pool, redis=redis, kafka=kafka, settings=_Settings())

    await drainer.drain_once()
    again = await drainer.drain_once()  # không còn PENDING
    assert again == 0
    assert await redis.llen("audit_chain:blocks") == 1  # chỉ 1 block


async def test_drainer_retry_keeps_pending_on_failure(pool, redis, monkeypatch):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_tier(tier="assist", actor="op")
    kafka = _FakeKafka()
    drainer = CratOutboxDrainer(pool, redis=redis, kafka=kafka, settings=_Settings())

    import services.admin_config.drainer as dmod
    async def boom(**kw):
        raise RuntimeError("crat down")
    monkeypatch.setattr(dmod, "write_audit_block", boom)

    sent = await drainer.drain_once()
    assert sent == 0
    ob = pool.store.crat_outbox[0]
    assert ob["status"] == "PENDING"  # < max_attempts → giữ PENDING để retry
    assert ob["attempts"] == 1
    assert "crat down" in ob["last_error"]


async def test_drainer_marks_failed_after_max_attempts(pool, redis, monkeypatch):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_tier(tier="assist", actor="op")
    kafka = _FakeKafka()
    drainer = CratOutboxDrainer(pool, redis=redis, kafka=kafka, settings=_Settings())

    import services.admin_config.drainer as dmod
    async def boom(**kw):
        raise RuntimeError("crat down")
    monkeypatch.setattr(dmod, "write_audit_block", boom)

    for _ in range(3):  # max_attempts=3
        await drainer.drain_once()
    assert pool.store.crat_outbox[0]["status"] == "FAILED"


# ── §6.7 new methods: risk-class invariant, flags/tenant/api-key/hitl ─────────
async def test_set_risk_class_blocks_dangerous_downgrade(pool, redis):
    """Bất biến: dangerous_tool không được hạ dưới HIGH dù version nào."""
    repo = AdminConfigRepo(pool, redis=redis)
    with pytest.raises(ValueError, match="bắt buộc HIGH"):
        await repo.set_risk_class_override(
            tool_name="k8s_delete_pod", risk_class="LOW", actor="op",
        )
    assert ("default", "k8s_delete_pod") not in pool.store.risk_override


async def test_list_runtime_flags_and_overrides(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.set_runtime_flag(flag_key="hitl_timeout", flag_value=900, value_type="int", actor="op")
    await repo.set_risk_class_override(tool_name="k8s_scale_resource", risk_class="HIGH", actor="op")
    flags = await repo.list_runtime_flags()
    assert any(f["flag_key"] == "hitl_timeout" and f["flag_value"] == 900 for f in flags)
    overrides = await repo.list_risk_class_overrides()
    assert overrides["k8s_scale_resource"]["risk_class"] == "HIGH"


async def test_create_tenant_and_api_key_then_revoke(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.create_tenant(tenant_id="acme", display_name="Acme", actor="admin")
    assert pool.store.tenant["acme"]["status"] == "active"
    key = await repo.create_api_key(
        tenant_id="acme", key_hash="h", key_prefix="abcd1234", actor="admin",
    )
    keys = await repo.list_api_keys("acme")
    assert keys[0]["key_prefix"] == "abcd1234" and keys[0]["status"] == "active"
    await repo.revoke_api_key(key_id=key["id"], actor="admin", tenant_id="acme")
    assert pool.store.api_key[key["id"]]["status"] == "revoked"
    # audit + outbox cho mỗi write (create tenant, create key, revoke key)
    assert len([o for o in pool.store.crat_outbox if "tenant" in o["dedup_key"] or "api_key" in o["dedup_key"]]) == 3


async def test_create_tenant_rejects_duplicate(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    await repo.create_tenant(tenant_id="dup", display_name="Dup", actor="admin")
    with pytest.raises(ValueError, match="đã tồn tại"):
        await repo.create_tenant(tenant_id="dup", display_name="Dup2", actor="admin")


async def test_decide_hitl_idempotent_and_enqueues_outbox(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    pool.store.hitl["p1"] = {
        "pending_id": "p1", "tenant_id": "default", "tool_name": "k8s_scale_deployment",
        "risk_class": "MEDIUM", "tier_at_time": "assist", "decision": "PENDING",
        "channel": "telegram", "actor": None, "created_at": None, "decided_at": None,
    }
    out = await repo.decide_hitl(pending_id="p1", decision="APPROVED", actor="op")
    assert out["tool_name"] == "k8s_scale_deployment"
    assert pool.store.hitl["p1"]["decision"] == "APPROVED"
    assert any(o["event_type"] == "HITL_DECISION" for o in pool.store.crat_outbox)
    # quyết định lại → chặn (không ghi đè)
    with pytest.raises(ValueError, match="đã quyết định"):
        await repo.decide_hitl(pending_id="p1", decision="REJECTED", actor="op")


async def test_decide_hitl_rejects_unknown_pending(pool, redis):
    repo = AdminConfigRepo(pool, redis=redis)
    with pytest.raises(ValueError, match="không tồn tại"):
        await repo.decide_hitl(pending_id="ghost", decision="APPROVED", actor="op")
