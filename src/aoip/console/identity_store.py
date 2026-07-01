"""PostgreSQL nguồn-sự-thật cho human portal identity + mirror sang Redis hot-path.

Kiến trúc (theo chỉ thị Slice 0):
  - PG (schema omni_admin, migration 0004) = source-of-truth BỀN VỮNG cho
    portal_user / provider_role / tenant_membership / support_access / auth_audit.
  - Redis = session opaque + revocation cache + provisioning mirror (hot-path resolve
    trong identity.py). "Redis may remain the active server-session store."
  - Mọi write role/membership ghi PG rồi mirror ngay sang Redis ⇒ load_session()
    re-resolve mỗi request phản chiếu PG tức thời (thu hồi có hiệu lực ngay).
  - Khởi động: hydrate_from_pg() nạp lại toàn bộ Redis mirror từ PG ⇒ user/role/
    membership sống sót restart Redis lẫn app (DoD #7).

pool=None (không có DSN) ⇒ store disabled, caller dùng identity.py provisioning trực
tiếp trên Redis (đường test/lab). PG có mặt ⇒ PG dẫn dắt.
"""
from __future__ import annotations

import time
from typing import Any

from aoip.console import identity


async def _mirror_user(redis, row: dict) -> None:
    await identity.upsert_user(
        redis, subject=row["subject"], email=row["email"],
        display_name=row.get("display_name") or row["email"],
        disabled=bool(row.get("disabled")),
    )


async def hydrate_from_pg(pool: Any, redis) -> dict[str, int]:
    """Nạp toàn bộ portal identity từ PG → Redis mirror. Gọi lúc khởi động.

    Trả số lượng đã nạp để log/health. Idempotent.
    """
    counts = {"users": 0, "provider_roles": 0, "memberships": 0}
    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT subject, email, display_name, disabled FROM omni_admin.portal_user")
        for u in users:
            await _mirror_user(redis, dict(u))
            counts["users"] += 1
        proles = await conn.fetch(
            "SELECT subject, role FROM omni_admin.provider_role_assignment")
        for r in proles:
            await identity.grant_provider_role(redis, subject=r["subject"], role=r["role"])
            counts["provider_roles"] += 1
        members = await conn.fetch(
            "SELECT subject, tenant_id, role FROM omni_admin.tenant_membership")
        for m in members:
            await identity.add_membership(
                redis, subject=m["subject"], tenant=m["tenant_id"], role=m["role"])
            counts["memberships"] += 1
    return counts


# ── writes: PG source-of-truth + mirror Redis (revocation có hiệu lực ngay) ──────
async def upsert_user(pool: Any, redis, *, subject: str, email: str,
                      display_name: str = "", disabled: bool = False) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO omni_admin.portal_user (subject, email, display_name, disabled)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (subject) DO UPDATE SET
                 email=EXCLUDED.email, display_name=EXCLUDED.display_name,
                 disabled=EXCLUDED.disabled, updated_at=now()""",
            subject, email, display_name or email, disabled)
    await identity.upsert_user(redis, subject=subject, email=email,
                               display_name=display_name, disabled=disabled)


async def set_user_disabled(pool: Any, redis, *, subject: str, disabled: bool,
                            actor: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE omni_admin.portal_user SET disabled=$2, updated_at=now() WHERE subject=$1",
            subject, disabled)
        await _audit_pg(conn, event="USER_DISABLED" if disabled else "USER_ENABLED",
                        subject=subject, detail=f"by {actor}")
    # Mirror: đọc lại email để giữ nguyên, chỉ đổi cờ disabled.
    async with pool.acquire() as conn:
        u = await conn.fetchrow(
            "SELECT email, display_name FROM omni_admin.portal_user WHERE subject=$1", subject)
    if u:
        await identity.upsert_user(redis, subject=subject, email=u["email"],
                                   display_name=u["display_name"], disabled=disabled)


async def grant_provider_role(pool: Any, redis, *, subject: str, role: str, actor: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO omni_admin.provider_role_assignment (subject, role, granted_by)
               VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""", subject, role, actor)
        await _audit_pg(conn, event="ROLE_GRANTED", subject=subject,
                        detail=f"provider:{role} by {actor}")
    await identity.grant_provider_role(redis, subject=subject, role=role)


async def revoke_provider_role(pool: Any, redis, *, subject: str, role: str, actor: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM omni_admin.provider_role_assignment WHERE subject=$1 AND role=$2",
            subject, role)
        await _audit_pg(conn, event="ROLE_REVOKED", subject=subject,
                        detail=f"provider:{role} by {actor}")
    await redis.srem(identity._PROVIDER_ROLES_K + subject, role)  # mirror: có hiệu lực ngay


async def add_membership(pool: Any, redis, *, subject: str, tenant: str, role: str,
                         actor: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO omni_admin.tenant_membership (subject, tenant_id, role, granted_by)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (subject, tenant_id) DO UPDATE SET role=EXCLUDED.role""",
            subject, tenant, role, actor)
        await _audit_pg(conn, event="MEMBERSHIP_GRANTED", subject=subject, tenant=tenant,
                        detail=f"{role} by {actor}")
    await identity.add_membership(redis, subject=subject, tenant=tenant, role=role)


async def remove_membership(pool: Any, redis, *, subject: str, tenant: str, actor: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM omni_admin.tenant_membership WHERE subject=$1 AND tenant_id=$2",
            subject, tenant)
        await _audit_pg(conn, event="MEMBERSHIP_REVOKED", subject=subject, tenant=tenant,
                        detail=f"by {actor}")
    await redis.hdel(identity._MEMBERSHIP + subject, tenant)  # mirror


# ── support access grant (raw evidence gate) ────────────────────────────────────
async def grant_support_access(pool: Any, *, subject: str, tenant: str, reason: str,
                               actor: str, ttl_s: int = 3600) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO omni_admin.support_access_grant
                 (subject, tenant_id, reason, granted_by, expires_at)
               VALUES ($1,$2,$3,$4, now() + ($5 || ' seconds')::interval)""",
            subject, tenant, reason, actor, str(ttl_s))
        await _audit_pg(conn, event="SUPPORT_GRANT", subject=subject, tenant=tenant,
                        detail=f"{reason} by {actor}")


async def has_active_support_grant(pool: Any, *, subject: str, tenant: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT 1 FROM omni_admin.support_access_grant
               WHERE subject=$1 AND tenant_id=$2 AND revoked_at IS NULL
                 AND expires_at > now() LIMIT 1""", subject, tenant)
    return row is not None


# ── durable audit (mirror của Redis edge-buffer) ────────────────────────────────
async def persist_audit(pool: Any, *, event: str, subject: str, tenant: str | None,
                        detail: str, ip: str = "", ua: str = "") -> None:
    async with pool.acquire() as conn:
        await _audit_pg(conn, event=event, subject=subject, tenant=tenant,
                        detail=detail, ip=ip, ua=ua)


async def _audit_pg(conn, *, event: str, subject: str, tenant: str | None = None,
                    detail: str = "", ip: str = "", ua: str = "") -> None:
    await conn.execute(
        """INSERT INTO omni_admin.portal_auth_audit (event, subject, tenant_id, detail, ip, ua)
           VALUES ($1,$2,$3,$4,$5,$6)""", event, subject, tenant, detail, ip, ua)
