"""Human identity cho portal — session server-side + membership resolver.

TÁCH BIỆT machine path (gateway/tenant_context.py). Ở đây CHỈ human portal user:

    OIDC subject → portal_user → provider_roles ∪ tenant_memberships → Principal → session

Nguyên tắc cứng:
  - Session là OPAQUE server-side (Redis key `portal:session:{sid}`), cookie chỉ giữ sid
    HttpOnly. Hỗ trợ logout/revocation = xoá key. KHÔNG để token nhạy cảm ở client.
  - Principal (kind/roles/tenant) suy ra từ membership SERVER-SIDE, KHÔNG từ client.
  - Standards-based: subject là OIDC `sub` bất kỳ; KHÔNG phụ thuộc claim riêng Keycloak.
  - tenant portal: tenant = membership của session, client KHÔNG chọn tenant khác.

Đây là Derived runtime identity value, không noun ontology mới.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from aoip.console.authz import (
    KIND_PROVIDER,
    KIND_TENANT,
    Principal,
    _PROVIDER_ROLES,
    _TENANT_ROLES,
)

# Redis keyspace (human portal — tách prefix khỏi machine `omni:agent:*`).
_USER = "portal:user:"                 # hash: subject → {email, display_name, disabled}
_PROVIDER_ROLES_K = "portal:proles:"   # set:  subject → {provider role,...}
_MEMBERSHIP = "portal:membership:"     # hash: subject → {tenant_id: role, ...}
_SESSION = "portal:session:"           # hash: sid → {subject, kind, tenant, created_at, expires_at}
_AUDIT = "portal:auth_audit"           # list: JSON auth events (append-only edge buffer)

SESSION_TTL_S = 8 * 3600               # 8h; refresh on activity ở lớp trên nếu cần


@dataclass(frozen=True)
class SessionInfo:
    sid: str
    subject: str
    principal: Principal
    expires_at: float


async def audit(redis, *, event: str, subject: str, tenant: str | None = None,
                detail: str = "", ts: float, ip: str = "", ua: str = "") -> None:
    """Append-only auth/authz audit (login/logout/denied/support-access)."""
    await redis.rpush(_AUDIT, json.dumps({
        "ts": ts, "event": event, "subject": subject, "tenant": tenant,
        "detail": detail, "ip": ip, "ua": ua,
    }))


async def read_audit(redis, *, limit: int = 200) -> list[dict]:
    raw = await redis.lrange(_AUDIT, -limit, -1)
    return [json.loads(x) for x in raw]


# ── provisioning (thay cho migration seed; production đọc từ PG omni_admin) ────
async def upsert_user(redis, *, subject: str, email: str, display_name: str = "",
                      disabled: bool = False) -> None:
    await redis.hset(_USER + subject, mapping={
        "email": email, "display_name": display_name or email,
        "disabled": "1" if disabled else "0",
    })


async def grant_provider_role(redis, *, subject: str, role: str) -> None:
    if role not in _PROVIDER_ROLES:
        raise ValueError(f"unknown provider role: {role}")
    await redis.sadd(_PROVIDER_ROLES_K + subject, role)


async def add_membership(redis, *, subject: str, tenant: str, role: str) -> None:
    if role not in _TENANT_ROLES:
        raise ValueError(f"unknown tenant role: {role}")
    await redis.hset(_MEMBERSHIP + subject, tenant, role)


# ── resolve subject → Principal(s) (SERVER-SIDE, membership-driven) ────────────
async def _is_active(redis, subject: str) -> bool:
    u = await redis.hgetall(_USER + subject)
    return bool(u) and u.get("disabled") != "1"


async def resolve_provider_principal(redis, subject: str) -> Principal | None:
    """subject → provider Principal nếu có provider role; else None."""
    if not await _is_active(redis, subject):
        return None
    roles = tuple(sorted(await redis.smembers(_PROVIDER_ROLES_K + subject)))
    if not roles:
        return None
    return Principal(subject=subject, kind=KIND_PROVIDER, roles=roles, tenant=None)


async def resolve_tenant_principal(redis, subject: str, tenant: str) -> Principal | None:
    """subject + tenant → tenant Principal nếu có membership ĐÚNG tenant đó; else None.

    tenant PHẢI là tenant user thuộc về (server-side). Không membership → None → 403.
    """
    if not await _is_active(redis, subject):
        return None
    role = await redis.hget(_MEMBERSHIP + subject, tenant)
    if not role:
        return None
    return Principal(subject=subject, kind=KIND_TENANT, roles=(role,), tenant=tenant)


async def list_memberships(redis, subject: str) -> dict[str, str]:
    """{tenant_id: role} — để chọn active org phía UI (chỉ trong phạm vi được phép)."""
    return dict(await redis.hgetall(_MEMBERSHIP + subject))


# ── session server-side (opaque sid; hỗ trợ revocation) ───────────────────────
def _new_sid(subject: str, kind: str, tenant: str | None, now: float) -> str:
    import hashlib
    raw = f"{subject}|{kind}|{tenant or '-'}|{now}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def issue_session(redis, *, principal: Principal, now: float,
                        ttl_s: int = SESSION_TTL_S) -> SessionInfo:
    """Phát hành session sau khi OIDC callback xác thực subject (lớp trên gọi).

    Session cột chặt kind/tenant tại thời điểm đăng nhập → request sau không đổi được scope.
    """
    sid = _new_sid(principal.subject, principal.kind, principal.tenant, now)
    exp = now + ttl_s
    await redis.hset(_SESSION + sid, mapping={
        "subject": principal.subject, "kind": principal.kind,
        "tenant": principal.tenant or "", "created_at": now, "expires_at": exp,
    })
    await redis.expire(_SESSION + sid, ttl_s)
    await audit(redis, event="LOGIN", subject=principal.subject,
                tenant=principal.tenant, detail=f"kind={principal.kind}", ts=now)
    return SessionInfo(sid=sid, subject=principal.subject, principal=principal, expires_at=exp)


async def load_session(redis, sid: str | None, now: float) -> SessionInfo | None:
    """Xác thực sid → SessionInfo (re-resolve roles server-side mỗi request).

    Re-resolve để thu hồi role có hiệu lực ngay (không tin snapshot trong session).
    """
    if not sid:
        return None
    s = await redis.hgetall(_SESSION + sid)
    if not s:
        return None
    if float(s.get("expires_at", 0)) <= now:
        await redis.delete(_SESSION + sid)
        return None
    subject, kind, tenant = s["subject"], s["kind"], (s.get("tenant") or None)
    if kind == KIND_PROVIDER:
        p = await resolve_provider_principal(redis, subject)
    else:
        p = await resolve_tenant_principal(redis, subject, tenant) if tenant else None
    if p is None:  # role/membership bị thu hồi → session vô hiệu
        await redis.delete(_SESSION + sid)
        return None
    return SessionInfo(sid=sid, subject=subject, principal=p,
                       expires_at=float(s["expires_at"]))


async def revoke_session(redis, sid: str | None, *, now: float) -> None:
    if not sid:
        return
    s = await redis.hgetall(_SESSION + sid)
    await redis.delete(_SESSION + sid)
    if s:
        await audit(redis, event="LOGOUT", subject=s.get("subject", "?"),
                    tenant=(s.get("tenant") or None), ts=now)
