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
_VALID_SUPPORT_TIERS = ("standard", "premium", "enterprise")
_VALID_ENVIRONMENT_TYPES = ("production", "staging", "development")
_VALID_ENVIRONMENT_STATUSES = ("onboarding", "active", "suspended", "archived")
_ENVIRONMENT_TRANSITIONS = {
    "onboarding": frozenset({"active", "suspended", "archived"}),
    "active": frozenset({"suspended", "archived"}),
    "suspended": frozenset({"active", "archived"}),
    "archived": frozenset(),
}


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

    async def get_tenant_plan(self, tenant_id: str = "default") -> dict[str, Any] | None:
        """Read tenant entitlements; missing row is unavailable, never unlimited."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT plan_code, agent_limit, autonomy_ceiling, retention_days, "
                "support_tier, enabled, updated_by, updated_at, version "
                "FROM omni_admin.tenant_plan WHERE tenant_id = $1",
                tenant_id,
            )
        if row is None:
            return None
        return {
            "tenant_id": tenant_id,
            "plan_code": row["plan_code"],
            "agent_limit": int(row["agent_limit"]),
            "autonomy_ceiling": row["autonomy_ceiling"],
            "retention_days": int(row["retention_days"]),
            "support_tier": row["support_tier"],
            "enabled": bool(row["enabled"]),
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "version": int(row["version"]),
        }

    async def get_autonomy_ceiling(self, tenant_id: str = "default") -> str | None:
        plan = await self.get_tenant_plan(tenant_id)
        return plan["autonomy_ceiling"] if plan else None

    async def list_tenant_plans(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tenant_id, plan_code, agent_limit, autonomy_ceiling, retention_days, "
                "support_tier, enabled, updated_by, updated_at, version "
                "FROM omni_admin.tenant_plan ORDER BY tenant_id"
            )
        return [
            {"tenant_id": r["tenant_id"], "plan_code": r["plan_code"],
             "agent_limit": int(r["agent_limit"]), "autonomy_ceiling": r["autonomy_ceiling"],
             "retention_days": int(r["retention_days"]), "support_tier": r["support_tier"],
             "enabled": bool(r["enabled"]), "updated_by": r["updated_by"],
             "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
             "version": int(r["version"])}
        for r in rows
        ]

    async def set_tenant_plan(
        self, *, tenant_id: str, plan_code: str, agent_limit: int,
        autonomy_ceiling: str, retention_days: int, support_tier: str,
        enabled: bool, actor: str,
    ) -> dict[str, Any]:
        if not plan_code.strip() or len(plan_code) > 64:
            raise ValueError("plan_code phải dài 1-64 ký tự")
        if agent_limit < 0 or retention_days <= 0:
            raise ValueError("agent_limit phải >= 0 và retention_days phải > 0")
        if autonomy_ceiling not in _VALID_TIERS:
            raise ValueError(f"autonomy_ceiling không hợp lệ: {autonomy_ceiling!r}")
        if support_tier not in _VALID_SUPPORT_TIERS:
            raise ValueError(f"support_tier không hợp lệ: {support_tier!r}")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                tenant_exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if not tenant_exists:
                    raise ValueError(f"tenant {tenant_id!r} không tồn tại")
                prev = await conn.fetchrow(
                    "SELECT plan_code, agent_limit, autonomy_ceiling, retention_days, "
                    "support_tier, enabled, version FROM omni_admin.tenant_plan "
                    "WHERE tenant_id = $1 FOR UPDATE",
                    tenant_id,
                )
                version = int(prev["version"]) + 1 if prev else 1
                await conn.execute(
                    "INSERT INTO omni_admin.tenant_plan "
                    "(tenant_id, plan_code, agent_limit, autonomy_ceiling, retention_days, "
                    "support_tier, enabled, updated_by, version) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET plan_code=$2, agent_limit=$3, "
                    "autonomy_ceiling=$4, retention_days=$5, support_tier=$6, enabled=$7, "
                    "updated_by=$8, updated_at=now(), version=$9",
                    tenant_id, plan_code.strip(), agent_limit, autonomy_ceiling,
                    retention_days, support_tier, enabled, actor, version,
                )
                new_value = {"plan_code": plan_code.strip(), "agent_limit": agent_limit,
                             "autonomy_ceiling": autonomy_ceiling, "retention_days": retention_days,
                             "support_tier": support_tier, "enabled": enabled}
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="tenant_plan", entity_key=tenant_id,
                    action="update", old_value=dict(prev or {}), new_value=new_value,
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"tenant_plan:{tenant_id}:{version}",
                    payload={"entity": "tenant_plan", "tenant_id": tenant_id,
                             **new_value, "actor": actor},
                )
        return {"tenant_id": tenant_id, **new_value, "version": version}

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

    async def list_environments(self, tenant_id: str) -> list[dict[str, Any]]:
        """List provider-managed environments for one tenant."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT environment_id, tenant_id, display_name, environment_type, "
                "status, created_at, updated_at "
                "FROM omni_admin.environment WHERE tenant_id = $1 "
                "ORDER BY created_at, environment_id",
                tenant_id,
            )
        return [
            {
                "environment_id": r["environment_id"],
                "tenant_id": r["tenant_id"],
                "display_name": r["display_name"],
                "environment_type": r["environment_type"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]

    async def create_environment(
        self, *, tenant_id: str, environment_id: str, display_name: str,
        environment_type: str, actor: str,
    ) -> dict[str, Any]:
        """Create an environment in onboarding state with an audited transaction."""
        if environment_type not in _VALID_ENVIRONMENT_TYPES:
            raise ValueError(
                f"environment_type không hợp lệ: {environment_type!r} "
                f"(cho phép {_VALID_ENVIRONMENT_TYPES})"
            )
        if not environment_id.strip() or len(environment_id) > 128:
            raise ValueError("environment_id phải dài 1-128 ký tự")
        if not display_name.strip():
            raise ValueError("display_name không được rỗng")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                tenant_exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if not tenant_exists:
                    raise ValueError(f"tenant {tenant_id!r} không tồn tại")
                exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.environment "
                    "WHERE tenant_id = $1 AND environment_id = $2",
                    tenant_id, environment_id,
                )
                if exists:
                    raise ValueError(
                        f"environment {environment_id!r} đã tồn tại trong tenant {tenant_id!r}"
                    )
                await conn.execute(
                    "INSERT INTO omni_admin.environment "
                    "(environment_id, tenant_id, display_name, environment_type) "
                    "VALUES ($1,$2,$3,$4)",
                    environment_id, tenant_id, display_name, environment_type,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="environment",
                    entity_key=environment_id, action="create", old_value={},
                    new_value={"environment_id": environment_id, "display_name": display_name,
                               "environment_type": environment_type, "status": "onboarding"},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"environment:{tenant_id}:{environment_id}:create",
                    payload={"entity": "environment", "tenant_id": tenant_id,
                             "environment_id": environment_id, "display_name": display_name,
                             "environment_type": environment_type, "actor": actor},
                )
        return {
            "tenant_id": tenant_id, "environment_id": environment_id,
            "display_name": display_name, "environment_type": environment_type,
            "status": "onboarding",
        }

    async def set_environment_status(
        self, *, tenant_id: str, environment_id: str, status: str, actor: str,
    ) -> dict[str, Any]:
        """Apply the explicit environment lifecycle; archived is terminal."""
        if status not in _VALID_ENVIRONMENT_STATUSES:
            raise ValueError(f"status không hợp lệ: {status!r}")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT display_name, environment_type, status "
                    "FROM omni_admin.environment WHERE tenant_id = $1 "
                    "AND environment_id = $2 FOR UPDATE",
                    tenant_id, environment_id,
                )
                if prev is None:
                    raise ValueError(f"environment {environment_id!r} không tồn tại")
                if status not in _ENVIRONMENT_TRANSITIONS[prev["status"]]:
                    raise ValueError(
                        f"không thể chuyển environment {environment_id!r} "
                        f"từ {prev['status']!r} sang {status!r}"
                    )
                await conn.execute(
                    "UPDATE omni_admin.environment SET status=$3, updated_at=now() "
                    "WHERE tenant_id=$1 AND environment_id=$2",
                    tenant_id, environment_id, status,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="environment",
                    entity_key=environment_id, action="update",
                    old_value={"status": prev["status"]}, new_value={"status": status},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"environment:{tenant_id}:{environment_id}:status:{status}:{_now_token()}",
                    payload={"entity": "environment", "tenant_id": tenant_id,
                             "environment_id": environment_id, "status": status,
                             "actor": actor},
                )
        return {"tenant_id": tenant_id, "environment_id": environment_id, "status": status}

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
        self, *, tenant_id: str, display_name: str, actor: str, idempotent: bool = False,
    ) -> dict[str, Any]:
        """Create a tenant. By default raises ValueError on duplicate (existing
        API contract — gateway maps this to HTTP 409). Pass ``idempotent=True``
        for repeatable provisioning callers (e.g. onboarding replay tooling)
        that must be safe to re-run without failing or duplicating state — in
        that mode an existing tenant is returned as-is (no row change, no new
        audit event) instead of raising.
        """
        tenant_id = tenant_id.strip()
        display_name = display_name.strip()
        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("tenant_id phải dài 1-128 ký tự")
        if not display_name or len(display_name) > 256:
            raise ValueError("display_name phải dài 1-256 ký tự")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if exists:
                    if idempotent:
                        # Repeatable-provisioning path: no row mutation, no new audit
                        # event — the caller already has a tenant to work with.
                        return {"tenant_id": tenant_id, "display_name": display_name}
                    raise ValueError(f"tenant {tenant_id!r} đã tồn tại")
                await conn.execute(
                    "INSERT INTO omni_admin.tenant (tenant_id, display_name) VALUES ($1,$2)",
                    tenant_id, display_name,
                )
                # A tenant without an entitlement row cannot enroll agents or resolve
                # autonomy safely. Create the bounded default in the same transaction
                # so the UI never creates an operationally unusable tenant.
                await conn.execute(
                    "INSERT INTO omni_admin.tenant_plan (tenant_id) VALUES ($1)",
                    tenant_id,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="tenant", entity_key=tenant_id,
                    action="create", old_value={}, new_value={"display_name": display_name},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"tenant:{tenant_id}:create",
                    payload={"entity": "tenant", "tenant_id": tenant_id,
                             "display_name": display_name,
                             "default_plan": {"plan_code": "standard", "agent_limit": 10,
                                              "autonomy_ceiling": "assist", "retention_days": 30,
                                              "support_tier": "standard", "enabled": True},
                             "actor": actor},
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

    # ---- agent enrollment (IT-3): one-time token → per-agent credential ----

    async def create_enroll_token(
        self, *, tenant_id: str, token_hash: str, token_prefix: str, actor: str,
        label: str | None = None, expires_at: Any = None,
        environment_id: str | None = None,
    ) -> dict[str, Any]:
        """Phát one-time enroll token cho tenant. Gotcha FK: tenant phải tồn tại
        trước (post-mortem drift-correction-2026-07-02) — check tường minh để trả
        lỗi rõ ràng thay vì FK violation."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM omni_admin.tenant WHERE tenant_id = $1", tenant_id,
                )
                if not exists:
                    raise ValueError(f"tenant {tenant_id!r} không tồn tại")
                if environment_id is not None:
                    env = await conn.fetchrow(
                        "SELECT status FROM omni_admin.environment "
                        "WHERE tenant_id = $1 AND environment_id = $2",
                        tenant_id, environment_id,
                    )
                    if env is None:
                        raise ValueError(f"environment {environment_id!r} không tồn tại")
                    if env["status"] in ("suspended", "archived"):
                        raise ValueError(f"environment {environment_id!r} không hoạt động")
                token_id = await conn.fetchval(
                    "INSERT INTO omni_admin.agent_enroll_token "
                    "(tenant_id, environment_id, token_hash, token_prefix, label, created_by, expires_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                    tenant_id, environment_id, token_hash, token_prefix, label, actor, expires_at,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="agent_enroll_token",
                    entity_key=str(token_id), action="create", old_value={},
                    new_value={"token_prefix": token_prefix},
                    actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"enroll_token:{tenant_id}:{token_id}:create",
                    payload={"entity": "agent_enroll_token", "tenant_id": tenant_id,
                             "token_id": token_id, "token_prefix": token_prefix,
                             "environment_id": environment_id,
                             "actor": actor},
                )
        return {"id": token_id, "token_prefix": token_prefix, "tenant_id": tenant_id,
                "environment_id": environment_id}

    async def consume_enroll_token_and_issue_credential(
        self, *, token_hash: str, agent_id: str, hostname: str,
        key_hash: str, key_prefix: str,
    ) -> dict[str, Any] | None:
        """Đổi enroll token lấy per-agent credential — MỘT transaction atomic.

        UPDATE có điều kiện status='issued' là cơ chế single-use: request thứ hai
        cùng token (kể cả race song song) không match được row nào → None → 401.
        Re-enroll cùng (tenant, agent) revoke credential active cũ trước khi cấp
        mới (unique partial index ux_agent_credential_active).
        Trả None khi token không tồn tại / đã dùng / revoked / hết hạn.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE omni_admin.agent_enroll_token "
                    "SET status='used', used_at=now(), used_by_agent=$2 "
                    "WHERE token_hash = $1 AND status = 'issued' "
                    "AND (expires_at IS NULL OR expires_at > now()) "
                    "RETURNING id, tenant_id, environment_id",
                    token_hash, agent_id,
                )
                if row is None:
                    return None
                token_id, tenant_id = row["id"], row["tenant_id"]
                environment_id = row["environment_id"]
                plan = await conn.fetchrow(
                    "SELECT agent_limit, enabled FROM omni_admin.tenant_plan "
                    "WHERE tenant_id = $1 FOR UPDATE",
                    tenant_id,
                )
                if plan is None or not bool(plan["enabled"]):
                    raise ValueError(f"tenant {tenant_id!r} không có entitlement agent hoạt động")
                active_agents = await conn.fetchval(
                    "SELECT COUNT(*) FROM omni_admin.agent_credential "
                    "WHERE tenant_id = $1 AND agent_id <> $2 AND status = 'active'",
                    tenant_id, agent_id,
                )
                if int(active_agents or 0) >= int(plan["agent_limit"]):
                    raise ValueError(
                        f"tenant {tenant_id!r} đã đạt giới hạn agent ({int(plan['agent_limit'])})"
                    )
                await conn.execute(
                    "UPDATE omni_admin.agent_credential "
                    "SET status='revoked', revoked_at=now() "
                    "WHERE tenant_id=$1 AND agent_id=$2 AND status='active'",
                    tenant_id, agent_id,
                )
                cred_id = await conn.fetchval(
                    "INSERT INTO omni_admin.agent_credential "
                    "(tenant_id, environment_id, agent_id, hostname, key_hash, key_prefix, enrolled_via_token) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                    tenant_id, environment_id, agent_id, hostname, key_hash, key_prefix, token_id,
                )
                await self._log_and_enqueue(
                    conn, tenant_id=tenant_id, entity="agent_credential",
                    entity_key=str(cred_id), action="create", old_value={},
                    new_value={"agent_id": agent_id, "key_prefix": key_prefix,
                               "enrolled_via_token": token_id, "environment_id": environment_id},
                    actor=f"enroll:{agent_id}", event_type=CRAT_EVENT_CONFIG_CHANGED,
                    dedup_key=f"agent_credential:{tenant_id}:{cred_id}:create",
                    payload={"entity": "agent_credential", "tenant_id": tenant_id,
                             "agent_id": agent_id, "credential_id": cred_id,
                             "environment_id": environment_id,
                             "key_prefix": key_prefix, "token_id": token_id},
                )
        return {"credential_id": cred_id, "tenant_id": tenant_id,
                "agent_id": agent_id, "key_prefix": key_prefix,
                "environment_id": environment_id}

    async def lookup_agent_credential(self, key_hash: str) -> dict[str, Any] | None:
        """Auth hot-path: hash → (tenant_id, agent_id) nếu credential active."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id, agent_id, environment_id FROM omni_admin.agent_credential "
                "WHERE key_hash = $1 AND status = 'active'",
                key_hash,
            )
        if row is None:
            return None
        return {"tenant_id": row["tenant_id"], "agent_id": row["agent_id"],
                "environment_id": row["environment_id"]}

    async def revoke_agent_credentials(
        self, *, tenant_id: str, agent_id: str, actor: str,
    ) -> list[str]:
        """Revoke mọi credential active của (tenant, agent). Trả list key_hash
        vừa revoke để caller xoá Redis auth-cache (401 hiệu lực tức thì)."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "UPDATE omni_admin.agent_credential "
                    "SET status='revoked', revoked_at=now() "
                    "WHERE tenant_id=$1 AND agent_id=$2 AND status='active' "
                    "RETURNING id, key_hash",
                    tenant_id, agent_id,
                )
                for row in rows:
                    await self._log_and_enqueue(
                        conn, tenant_id=tenant_id, entity="agent_credential",
                        entity_key=str(row["id"]), action="delete",
                        old_value={"status": "active"}, new_value={"status": "revoked"},
                        actor=actor, event_type=CRAT_EVENT_CONFIG_CHANGED,
                        dedup_key=f"agent_credential:{tenant_id}:{row['id']}:revoke",
                        payload={"entity": "agent_credential", "tenant_id": tenant_id,
                                 "agent_id": agent_id, "credential_id": row["id"],
                                 "actor": actor},
                    )
        return [row["key_hash"] for row in rows]

    async def list_agent_credentials(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, agent_id, hostname, environment_id, key_prefix, status, created_at, revoked_at "
                "FROM omni_admin.agent_credential WHERE tenant_id = $1 ORDER BY id",
                tenant_id,
            )
        return [
            {"id": r["id"], "agent_id": r["agent_id"], "hostname": r["hostname"],
             "environment_id": r["environment_id"],
             "key_prefix": r["key_prefix"], "status": r["status"],
             "created_at": str(r["created_at"] or ""),
             "revoked_at": str(r["revoked_at"] or "")}
            for r in rows
        ]

    async def get_or_claim_agent_owner(self, agent_id: str, tenant_id: str) -> str:
        """Durable, no-TTL first-claim-wins agent_id ownership (Phase 3, 0-6
        roadmap — migrations/omni_admin/0010_agent_identity_claim.sql).

        Closes a gap the ephemeral Redis registry (TTL=120s) cannot: for
        tenant-shared-key deployments (no per-agent credential row to bind
        ownership durably — see agent_credential/0005), a registry TTL expiry
        used to mean ANY tenant's key could re-claim that agent_id string
        until the original host re-registered. This table remembers the
        FIRST tenant to ever claim a given agent_id, permanently — a
        different tenant can never claim it later, even across arbitrarily
        long outages.

        Atomic: INSERT ... ON CONFLICT DO NOTHING, single round-trip, race-safe
        under concurrent first-registration attempts. Always returns the
        durable owner tenant_id (the caller's own tenant_id if this call just
        claimed it, or the existing owner's tenant_id if already claimed).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO omni_admin.agent_identity_claim (agent_id, tenant_id) "
                "VALUES ($1, $2) ON CONFLICT (agent_id) DO NOTHING "
                "RETURNING tenant_id",
                agent_id, tenant_id,
            )
            if row is not None:
                return row["tenant_id"]
            owner = await conn.fetchrow(
                "SELECT tenant_id FROM omni_admin.agent_identity_claim WHERE agent_id = $1",
                agent_id,
            )
        return owner["tenant_id"] if owner is not None else tenant_id

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
