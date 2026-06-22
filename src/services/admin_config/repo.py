"""Async repo cho schema omni_admin — Transactional Outbox + write-through cache.

Mọi write config đi qua 1 transaction atomic:
    UPSERT bảng đích (version+1) + INSERT config_change_log + INSERT crat_outbox
→ COMMIT → write-through Redis cache. CRAT block do CratOutboxDrainer ghi sau
(at-least-once, idempotent qua dedup_key UNIQUE). Postgres fail → TX rollback,
Redis KHÔNG bị đụng (fail-closed, nhất quán kill-switch/CRAT).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from services.admin_config import cache

logger = logging.getLogger(__name__)


def _now_token() -> int:
    """Mốc thời gian ms — phân biệt dedup_key cho thao tác lặp (suspend/active nhiều lần)."""
    return int(time.time() * 1000)

# Mirror services.audit_ledger.crat_event_types (canonical) — KHÔNG import từ đó để
# admin_config không kéo theo chain_writer/cryptography (gateway chỉ cần repo nhẹ).
CRAT_EVENT_AUTONOMY_TIER_CHANGED = "AUTONOMY_TIER_CHANGED"
CRAT_EVENT_CONFIG_CHANGED = "CONFIG_CHANGED"
CRAT_EVENT_HITL_DECISION = "HITL_DECISION"

_VALID_TIERS = ("shadow", "assist", "auto")


class OptimisticLockError(RuntimeError):
    """Version trong DB không khớp version kỳ vọng (ai đó ghi chen giữa)."""


class AdminConfigRepo:
    """Repo bám asyncpg pool. ``redis`` optional (write-through cache)."""

    def __init__(self, pool: Any, *, redis: Any = None) -> None:
        self._pool = pool
        self._redis = redis

    # ---- reads (hot-path: cache → Postgres → caller env default) -----------

    async def get_tier(self, tenant_id: str = "default") -> str | None:
        """Tier hiệu lực: cache trước, miss → Postgres. None = chưa có (env default)."""
        cached = await cache.read_tier_cached(self._redis, tenant_id)
        if cached:
            return cached
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tier FROM omni_admin.autonomy_tier_state WHERE tenant_id = $1",
                tenant_id,
            )
        if row is None:
            return None
        tier = row["tier"]
        await cache.write_through_cache(self._redis, cache.cache_key_tier(tenant_id), tier)
        return tier

    async def get_runtime_flag(self, flag_key: str, tenant_id: str = "default") -> Any | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT flag_value FROM omni_admin.runtime_flag "
                "WHERE tenant_id = $1 AND flag_key = $2",
                tenant_id,
                flag_key,
            )
        if row is None:
            return None
        return json.loads(row["flag_value"])

    async def get_tenant_readiness(self, tenant_id: str = "default") -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT endpoint_mapped_pct, business_flow_confirmed_pct, "
                "open_questions_over_threshold, readiness_flag, updated_at "
                "FROM omni_admin.tenant_readiness_state WHERE tenant_id = $1",
                tenant_id,
            )
        if row is None:
            return None
        return {
            "endpoint_mapped_pct": float(row["endpoint_mapped_pct"]) if row["endpoint_mapped_pct"] is not None else None,
            "business_flow_confirmed_pct": float(row["business_flow_confirmed_pct"]) if row["business_flow_confirmed_pct"] is not None else None,
            "open_questions_over_threshold": int(row["open_questions_over_threshold"]),
            "readiness_flag": bool(row["readiness_flag"]),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def get_tenant_telegram_chat_id(self, tenant_id: str = "default") -> int | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_chat_id FROM omni_admin.tenant WHERE tenant_id = $1",
                tenant_id,
            )
        return int(row["telegram_chat_id"]) if row and row["telegram_chat_id"] is not None else None

    async def get_risk_class_override(self, tool_name: str, tenant_id: str = "default") -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT risk_class FROM omni_admin.risk_class_override "
                "WHERE tenant_id = $1 AND tool_name = $2",
                tenant_id,
                tool_name,
            )
        return None if row is None else row["risk_class"]

    # ---- writes (atomic 3-in-1 TX + write-through cache) -------------------

    async def set_tier(
        self,
        *,
        tier: str,
        actor: str,
        tenant_id: str = "default",
        readiness: dict[str, Any] | None = None,
        forced: bool = False,
    ) -> dict[str, Any]:
        """Đổi tier atomic. Trả {tier, version, dedup_key}.

        1 TX: UPSERT autonomy_tier_state + INSERT autonomy_tier_history +
        config_change_log + crat_outbox(AUTONOMY_TIER_CHANGED). Cache write-through sau commit.
        """
        if tier not in _VALID_TIERS:
            raise ValueError(f"tier không hợp lệ: {tier!r} (cho phép {_VALID_TIERS})")
        readiness = readiness or {}
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT tier, version FROM omni_admin.autonomy_tier_state "
                    "WHERE tenant_id = $1 FOR UPDATE",
                    tenant_id,
                )
                from_tier = prev["tier"] if prev else None
                new_version = (prev["version"] + 1) if prev else 1
                await conn.execute(
                    "INSERT INTO omni_admin.autonomy_tier_state "
                    "(tenant_id, tier, updated_by, version) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "tier=EXCLUDED.tier, updated_by=EXCLUDED.updated_by, "
                    "updated_at=now(), version=$4",
                    tenant_id,
                    tier,
                    actor,
                    new_version,
                )
                await conn.execute(
                    "INSERT INTO omni_admin.autonomy_tier_history "
                    "(tenant_id, from_tier, to_tier, actor, wilson_lb, accepted, total, "
                    "elapsed_days, forced) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    tenant_id,
                    from_tier,
                    tier,
                    actor,
                    readiness.get("wilson_lb"),
                    readiness.get("accepted"),
                    readiness.get("total"),
                    readiness.get("elapsed_days"),
                    forced,
                )
                dedup_key = f"tier:{tenant_id}:{new_version}"
                payload = {
                    "from": from_tier,
                    "to": tier,
                    "actor": actor,
                    "tenant_id": tenant_id,
                    "forced": forced,
                    **{k: readiness.get(k) for k in ("wilson_lb", "accepted", "total", "elapsed_days")},
                }
                await self._log_and_enqueue(
                    conn,
                    tenant_id=tenant_id,
                    entity="tier",
                    entity_key=None,
                    action="update",
                    old_value={"tier": from_tier},
                    new_value={"tier": tier},
                    actor=actor,
                    event_type=CRAT_EVENT_AUTONOMY_TIER_CHANGED,
                    dedup_key=dedup_key,
                    payload=payload,
                )
        # post-commit write-through
        await cache.write_through_cache(self._redis, cache.cache_key_tier(tenant_id), tier)
        logger.info("admin_config: tier %s→%s tenant=%s v=%d actor=%s", from_tier, tier, tenant_id, new_version, actor)
        return {"tier": tier, "version": new_version, "dedup_key": dedup_key}

    async def set_runtime_flag(
        self,
        *,
        flag_key: str,
        flag_value: Any,
        value_type: str,
        actor: str,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        """Set runtime flag atomic + outbox(CONFIG_CHANGED). Invalidate cache key."""
        value_json = json.dumps(flag_value)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT flag_value, version FROM omni_admin.runtime_flag "
                    "WHERE tenant_id = $1 AND flag_key = $2 FOR UPDATE",
                    tenant_id,
                    flag_key,
                )
                old_value = json.loads(prev["flag_value"]) if prev else None
                new_version = (prev["version"] + 1) if prev else 1
                await conn.execute(
                    "INSERT INTO omni_admin.runtime_flag "
                    "(tenant_id, flag_key, flag_value, value_type, updated_by, version) "
                    "VALUES ($1,$2,$3::jsonb,$4,$5,$6) "
                    "ON CONFLICT (tenant_id, flag_key) DO UPDATE SET "
                    "flag_value=EXCLUDED.flag_value, value_type=EXCLUDED.value_type, "
                    "updated_by=EXCLUDED.updated_by, updated_at=now(), version=$6",
                    tenant_id,
                    flag_key,
                    value_json,
                    value_type,
                    actor,
                    new_version,
                )
                dedup_key = f"runtime_flag:{tenant_id}:{flag_key}:{new_version}"
                await self._log_and_enqueue(
                    conn,
                    tenant_id=tenant_id,
                    entity="runtime_flag",
                    entity_key=flag_key,
                    action="update",
                    old_value={"value": old_value},
                    new_value={"value": flag_value},
                    actor=actor,
                    event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=dedup_key,
                    payload={
                        "entity": "runtime_flag",
                        "flag_key": flag_key,
                        "old": old_value,
                        "new": flag_value,
                        "actor": actor,
                        "tenant_id": tenant_id,
                    },
                )
        await cache.invalidate_cache(
            self._redis, cache.cache_key_runtime_flag(tenant_id, flag_key)
        )
        return {"flag_key": flag_key, "version": new_version, "dedup_key": dedup_key}

    async def set_risk_class_override(
        self,
        *,
        tool_name: str,
        risk_class: str,
        actor: str,
        reason: str | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        """Override risk-class 1 tool atomic + outbox(CONFIG_CHANGED).

        BẤT BIẾN enforce ở caller/gate (không hạ dangerous_tools dưới HIGH); repo
        chỉ persist. Invalidate cache risk key.
        """
        if risk_class not in ("READONLY", "LOW", "MEDIUM", "HIGH"):
            raise ValueError(f"risk_class không hợp lệ: {risk_class!r}")
        # BẤT BIẾN: không hạ dangerous_tools xuống dưới HIGH. Import từ pkg (KHÔNG
        # workers) để gateway dùng được — bất biến gateway-no-workers.
        from pkg.risk_taxonomy import DANGEROUS_TOOLS

        if tool_name in DANGEROUS_TOOLS and risk_class != "HIGH":
            raise ValueError(
                f"dangerous_tool {tool_name!r} bắt buộc HIGH — không cho override xuống {risk_class!r}"
            )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT risk_class, version FROM omni_admin.risk_class_override "
                    "WHERE tenant_id = $1 AND tool_name = $2 FOR UPDATE",
                    tenant_id,
                    tool_name,
                )
                old_value = prev["risk_class"] if prev else None
                new_version = (prev["version"] + 1) if prev else 1
                await conn.execute(
                    "INSERT INTO omni_admin.risk_class_override "
                    "(tenant_id, tool_name, risk_class, reason, updated_by, version) "
                    "VALUES ($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT (tenant_id, tool_name) DO UPDATE SET "
                    "risk_class=EXCLUDED.risk_class, reason=EXCLUDED.reason, "
                    "updated_by=EXCLUDED.updated_by, updated_at=now(), version=$6",
                    tenant_id,
                    tool_name,
                    risk_class,
                    reason,
                    actor,
                    new_version,
                )
                dedup_key = f"risk_class:{tenant_id}:{tool_name}:{new_version}"
                await self._log_and_enqueue(
                    conn,
                    tenant_id=tenant_id,
                    entity="risk_class",
                    entity_key=tool_name,
                    action="update",
                    old_value={"risk_class": old_value},
                    new_value={"risk_class": risk_class},
                    actor=actor,
                    event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=dedup_key,
                    payload={
                        "entity": "risk_class",
                        "tool_name": tool_name,
                        "old": old_value,
                        "new": risk_class,
                        "reason": reason,
                        "actor": actor,
                        "tenant_id": tenant_id,
                    },
                )
        await cache.invalidate_cache(
            self._redis, cache.cache_key_risk_class(tenant_id, tool_name)
        )
        return {"tool_name": tool_name, "version": new_version, "dedup_key": dedup_key}

    async def set_tenant_readiness(
        self,
        *,
        tenant_id: str,
        endpoint_mapped_pct: float,
        business_flow_confirmed_pct: float,
        open_questions_over_threshold: int,
        readiness_flag: bool,
    ) -> dict[str, Any]:
        """Upsert readiness checklist (step-3 onboarding-ops-agent plan).

        Recomputed periodically by the onboarding worker — not a discrete admin
        action, so no config_change_log/crat_outbox entry here (step-4 task-4
        owns the CRAT event TENANT_READINESS_GATE_OPENED on the false→true edge).
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO omni_admin.tenant_readiness_state "
                "(tenant_id, endpoint_mapped_pct, business_flow_confirmed_pct, "
                "open_questions_over_threshold, readiness_flag) VALUES ($1,$2,$3,$4,$5) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "endpoint_mapped_pct=EXCLUDED.endpoint_mapped_pct, "
                "business_flow_confirmed_pct=EXCLUDED.business_flow_confirmed_pct, "
                "open_questions_over_threshold=EXCLUDED.open_questions_over_threshold, "
                "readiness_flag=EXCLUDED.readiness_flag, updated_at=now()",
                tenant_id,
                endpoint_mapped_pct,
                business_flow_confirmed_pct,
                open_questions_over_threshold,
                readiness_flag,
            )
        await cache.write_through_cache(
            self._redis, cache.cache_key_readiness(tenant_id), "true" if readiness_flag else "false", ttl=60,
        )
        return {
            "tenant_id": tenant_id,
            "endpoint_mapped_pct": endpoint_mapped_pct,
            "business_flow_confirmed_pct": business_flow_confirmed_pct,
            "open_questions_over_threshold": open_questions_over_threshold,
            "readiness_flag": readiness_flag,
        }

    async def set_tenant_telegram_chat_id(
        self, *, tenant_id: str, chat_id: int, actor: str,
    ) -> dict[str, Any]:
        """Set/đổi chat_id Telegram của 1 tenant (cho ask-loop A5) — audited write."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if not exists:
                    raise ValueError(f"tenant {tenant_id!r} không tồn tại")
                await conn.execute(
                    "UPDATE omni_admin.tenant SET telegram_chat_id=$2 WHERE tenant_id=$1",
                    tenant_id, chat_id,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="tenant_telegram_chat_id",
                    entity_key=tenant_id, action="update", old_value={},
                    new_value={"chat_id": chat_id}, actor=actor,
                    event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"tenant:{tenant_id}:telegram_chat_id:{chat_id}:{_now_token()}",
                    payload={"entity": "tenant_telegram_chat_id", "tenant_id": tenant_id,
                             "chat_id": chat_id, "actor": actor},
                )
        return {"tenant_id": tenant_id, "telegram_chat_id": chat_id}

    async def record_hitl_decision(
        self,
        *,
        pending_id: str,
        decision: str,
        actor: str,
        channel: str = "telegram",
        tenant_id: str = "default",
    ) -> None:
        """Cập nhật hitl_decision ledger (UI query nhanh; CRAT vẫn là chain bất biến)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE omni_admin.hitl_decision SET decision=$2, actor=$3, "
                "channel=$4, decided_at=now() WHERE pending_id=$1",
                pending_id,
                decision,
                actor,
                channel,
            )

    # ---- list reads (Admin UI matrices/tables) ----------------------------

    async def list_runtime_flags(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT flag_key, flag_value, value_type, updated_by, updated_at, version "
                "FROM omni_admin.runtime_flag WHERE tenant_id = $1 ORDER BY flag_key",
                tenant_id,
            )
        return [
            {
                "flag_key": r["flag_key"],
                "flag_value": json.loads(r["flag_value"]),
                "value_type": r["value_type"],
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "version": r["version"],
            }
            for r in rows
        ]

    async def list_risk_class_overrides(self, tenant_id: str = "default") -> dict[str, dict[str, Any]]:
        """Map tool_name → override row (chỉ tool đã override; bảng tĩnh ghép ở endpoint)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tool_name, risk_class, reason, updated_by, updated_at, version "
                "FROM omni_admin.risk_class_override WHERE tenant_id = $1",
                tenant_id,
            )
        return {
            r["tool_name"]: {
                "risk_class": r["risk_class"],
                "reason": r["reason"],
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "version": r["version"],
            }
            for r in rows
        }

    async def list_tenants(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT t.tenant_id, t.display_name, t.status, t.created_at, "
                "COUNT(k.id) FILTER (WHERE k.status='active') AS active_keys "
                "FROM omni_admin.tenant t "
                "LEFT JOIN omni_admin.tenant_api_key k ON k.tenant_id = t.tenant_id "
                "GROUP BY t.tenant_id ORDER BY t.created_at",
            )
        return [
            {
                "tenant_id": r["tenant_id"],
                "display_name": r["display_name"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "active_keys": int(r["active_keys"] or 0),
            }
            for r in rows
        ]

    async def list_api_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, key_prefix, label, status, created_by, created_at, revoked_at "
                "FROM omni_admin.tenant_api_key WHERE tenant_id = $1 ORDER BY created_at DESC",
                tenant_id,
            )
        return [
            {
                "id": r["id"],
                "key_prefix": r["key_prefix"],
                "label": r["label"],
                "status": r["status"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
            }
            for r in rows
        ]

    async def list_hitl_pending(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pending_id, tool_name, risk_class, tier_at_time, decision, "
                "channel, actor, created_at, decided_at FROM omni_admin.hitl_decision "
                "WHERE tenant_id = $1 AND decision = 'PENDING' ORDER BY created_at DESC LIMIT 100",
                tenant_id,
            )
        return [
            {
                "pending_id": r["pending_id"],
                "tool_name": r["tool_name"],
                "risk_class": r["risk_class"],
                "tier_at_time": r["tier_at_time"],
                "decision": r["decision"],
                "channel": r["channel"],
                "actor": r["actor"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
            }
            for r in rows
        ]

    async def decide_hitl(
        self, *, pending_id: str, decision: str, actor: str,
        channel: str = "ui", tenant_id: str = "default",
    ) -> dict[str, Any]:
        """Duyệt HITL trên UI — atomic: UPDATE ledger + outbox(HITL_DECISION).

        CRAT block do drainer ghi (TRƯỚC khi worker thực dispatch action: worker chỉ
        action sau khi đọc decision đã persist + CRAT đã enqueue). Trả tool_name để
        caller publish Kafka định tuyến APPROVED→omni-actions / REJECTED→feedback.
        """
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError(f"decision không hợp lệ: {decision!r}")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT tool_name, risk_class, tier_at_time, decision FROM "
                    "omni_admin.hitl_decision WHERE pending_id = $1 AND tenant_id = $2 "
                    "FOR UPDATE",
                    pending_id, tenant_id,
                )
                if prev is None:
                    raise ValueError(f"hitl pending {pending_id!r} không tồn tại")
                if prev["decision"] != "PENDING":
                    raise ValueError(
                        f"hitl {pending_id!r} đã quyết định ({prev['decision']}) — không ghi đè"
                    )
                await conn.execute(
                    "UPDATE omni_admin.hitl_decision SET decision=$2, actor=$3, "
                    "channel=$4, decided_at=now() WHERE pending_id=$1",
                    pending_id, decision, actor, channel,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="hitl", entity_key=pending_id,
                    action="update", old_value={"decision": "PENDING"},
                    new_value={"decision": decision}, actor=actor,
                    event_type=CRAT_EVENT_HITL_DECISION,
                    dedup_key=f"hitl:{pending_id}:{decision}",
                    payload={"entity": "hitl", "pending_id": pending_id,
                             "decision": decision, "tool_name": prev["tool_name"],
                             "risk_class": prev["risk_class"],
                             "tier_at_time": prev["tier_at_time"],
                             "channel": channel, "actor": actor, "tenant_id": tenant_id},
                )
        return {
            "pending_id": pending_id, "decision": decision,
            "tool_name": prev["tool_name"], "risk_class": prev["risk_class"],
        }

    # ---- tenant / api-key writes (audited via _log_and_enqueue) ------------

    async def create_tenant(
        self, *, tenant_id: str, display_name: str, actor: str,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if exists:
                    raise ValueError(f"tenant {tenant_id!r} đã tồn tại")
                await conn.execute(
                    "INSERT INTO omni_admin.tenant (tenant_id, display_name) VALUES ($1,$2)",
                    tenant_id, display_name,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="tenant", entity_key=tenant_id,
                    action="create", old_value={}, new_value={"display_name": display_name},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"tenant:{tenant_id}:create",
                    payload={"entity": "tenant", "tenant_id": tenant_id,
                             "display_name": display_name, "actor": actor},
                )
        return {"tenant_id": tenant_id, "display_name": display_name}

    async def set_tenant_status(
        self, *, tenant_id: str, status: str, actor: str,
    ) -> dict[str, Any]:
        if status not in ("active", "suspended"):
            raise ValueError(f"status không hợp lệ: {status!r}")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT status FROM omni_admin.tenant WHERE tenant_id = $1 FOR UPDATE",
                    tenant_id,
                )
                if prev is None:
                    raise ValueError(f"tenant {tenant_id!r} không tồn tại")
                await conn.execute(
                    "UPDATE omni_admin.tenant SET status=$2 WHERE tenant_id=$1",
                    tenant_id, status,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="tenant", entity_key=tenant_id,
                    action="update", old_value={"status": prev["status"]},
                    new_value={"status": status}, actor=actor,
                    event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"tenant:{tenant_id}:status:{status}:{_now_token()}",
                    payload={"entity": "tenant", "tenant_id": tenant_id,
                             "status": status, "actor": actor},
                )
        return {"tenant_id": tenant_id, "status": status}

    async def create_api_key(
        self, *, tenant_id: str, key_hash: str, key_prefix: str, actor: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if not exists:
                    raise ValueError(f"tenant {tenant_id!r} không tồn tại")
                key_id = await conn.fetchval(
                    "INSERT INTO omni_admin.tenant_api_key "
                    "(tenant_id, key_hash, key_prefix, label, created_by) "
                    "VALUES ($1,$2,$3,$4,$5) RETURNING id",
                    tenant_id, key_hash, key_prefix, label, actor,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="api_key", entity_key=str(key_id),
                    action="create", old_value={}, new_value={"key_prefix": key_prefix},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"api_key:{tenant_id}:{key_id}:create",
                    payload={"entity": "api_key", "tenant_id": tenant_id,
                             "key_id": key_id, "key_prefix": key_prefix, "actor": actor},
                )
        return {"id": key_id, "key_prefix": key_prefix}

    async def revoke_api_key(
        self, *, key_id: int, actor: str, tenant_id: str = "default",
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT tenant_id, status FROM omni_admin.tenant_api_key "
                    "WHERE id = $1 FOR UPDATE",
                    key_id,
                )
                if prev is None:
                    raise ValueError(f"api_key id={key_id} không tồn tại")
                await conn.execute(
                    "UPDATE omni_admin.tenant_api_key SET status='revoked', "
                    "revoked_at=now() WHERE id=$1",
                    key_id,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=prev["tenant_id"], entity="api_key",
                    entity_key=str(key_id), action="delete",
                    old_value={"status": prev["status"]}, new_value={"status": "revoked"},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"api_key:{prev['tenant_id']}:{key_id}:revoke",
                    payload={"entity": "api_key", "key_id": key_id,
                             "tenant_id": prev["tenant_id"], "actor": actor},
                )
        return {"id": key_id, "status": "revoked"}

    # ---- shared TX tail: config_change_log + crat_outbox ------------------

    async def _log_and_enqueue(
        self,
        conn: Any,
        *,
        tenant_id: str,
        entity: str,
        entity_key: str | None,
        action: str,
        old_value: dict[str, Any],
        new_value: dict[str, Any],
        actor: str,
        event_type: str,
        dedup_key: str,
        payload: dict[str, Any],
    ) -> None:
        """Ghi config_change_log + crat_outbox trong CÙNG TX (atomic audit + CRAT-intent)."""
        await conn.execute(
            "INSERT INTO omni_admin.config_change_log "
            "(tenant_id, entity, entity_key, action, old_value, new_value, actor) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7)",
            tenant_id,
            entity,
            entity_key,
            action,
            json.dumps(old_value),
            json.dumps(new_value),
            actor,
        )
        await conn.execute(
            "INSERT INTO omni_admin.crat_outbox "
            "(dedup_key, event_type, payload, status) VALUES ($1,$2,$3::jsonb,'PENDING') "
            "ON CONFLICT (dedup_key) DO NOTHING",
            dedup_key,
            event_type,
            json.dumps(payload),
        )
