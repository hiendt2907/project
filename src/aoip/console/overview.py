"""Provider Control Tower — aggregation control-tower từ NGUỒN THẬT.

Nguyên tắc: mỗi metric hoặc có `value` (nguồn thật) hoặc `available=False` + `reason` (khe hở
nguồn được nêu rõ). KHÔNG bịa số, KHÔNG hardcode. Đây là READ-MODEL mỏng: đọc lại Trace Spine
(incidents/approvals/reconcile/activity), Redis agent registry (fleet), PG omni_admin (tenants),
liveness dependencies (component health). Không tạo nguồn sự thật thứ hai.

Slice A phơi bày phần đã có nguồn; license store và version baseline chưa có nguồn thật vẫn trả
unavailable. Mission/onboarding đã có Redis read-model tenant-scoped.
"""
from __future__ import annotations

import json
import logging

from aoip.agent.trace import TERMINAL_EVENTS
from aoip.console.projections import provider_incident
from aoip.mission_store import MissionStore
from aoip.question_lifecycle import QUESTIONS_KEY, list_questions

logger = logging.getLogger(__name__)

_REMOTE_PREFIX = "omni:remote_agent:registry:"
_REMOTE_STALE_SEC = 120          # khớp gateway/routes/agents.py (_REMOTE_STALE_SEC)
_TRACE_INDEX_PREFIX = "trace:index:"
_RECENT_LIMIT = 15               # số event hoạt động gần nhất phơi ở Overview
_MAX_TENANTS_SCAN = 500          # chặn quét vô biên (lab-scale; provider phân trang ở B)


def _metric(value) -> dict:
    return {"available": True, "value": value}


def _gap(reason: str) -> dict:
    """Metric chưa có nguồn — nêu rõ khe hở + sub-slice sẽ lấp (không hiển thị số giả)."""
    return {"available": False, "reason": reason}


async def _agent_fleet(redis, now: float) -> dict:
    """online/offline từ Redis registry (online nếu now-last_seen ≤ 120s). Thật."""
    online = offline = 0
    try:
        keys = await redis.keys(f"{_REMOTE_PREFIX}*")
    except Exception as e:  # Redis không tới được → khe hở nguồn rõ ràng
        return _gap(f"agent registry (Redis) không truy cập được: {e}")
    for key in keys:
        raw = await redis.get(key)
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, TypeError):
            continue
        age = now - int(rec.get("last_seen", 0))
        if age <= _REMOTE_STALE_SEC:
            online += 1
        else:
            offline += 1
    return _metric({"online": online, "offline": offline, "total": online + offline})


async def _trace_rollup(redis, trace, now: float) -> dict:
    """Gộp Trace Spine mọi tenant: incidents active, reconcile-required, approvals, activity.

    Một vòng quét index để không đọc timeline nhiều lần. Tất cả là read-model thật.
    """
    active_incidents = 0
    reconcile_required = 0
    pending_approvals = 0
    recent: list[dict] = []
    try:
        index_keys = await redis.keys(f"{_TRACE_INDEX_PREFIX}*")
    except Exception as e:
        return {"error": _gap(f"Trace Spine (Redis) không truy cập được: {e}")}
    for ikey in sorted(index_keys)[:_MAX_TENANTS_SCAN]:
        tenant = ikey.split(_TRACE_INDEX_PREFIX, 1)[1]
        pending_approvals += len(await trace.pending_approvals(tenant))
        for cid in await trace.list_timelines(tenant):
            events = await trace.timeline(tenant, cid)
            if not events:
                continue
            view = provider_incident(events, include_raw=False)
            if not view.get("reported"):
                active_incidents += 1
            if view.get("reconcile_required") and not _is_reconciled(events):
                reconcile_required += 1
            last = events[-1]
            recent.append({
                "tenant": tenant, "correlation_id": cid,
                "incident_id": last.get("incident_id", ""),
                "event": last.get("event_type", ""),
                "reason": last.get("reason", ""),
                "timestamp": last.get("timestamp", 0),
            })
    recent.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)
    return {
        "active_incidents": _metric(active_incidents),
        "pending_approvals": _metric(pending_approvals),
        "reconcile_required": _metric(reconcile_required),
        "recent_activity": _metric(recent[:_RECENT_LIMIT]),
    }


def _is_reconciled(events: list[dict]) -> bool:
    for e in reversed(events):
        if e.get("event_type") == "RECONCILED":
            return True
        if e.get("event_type") in TERMINAL_EVENTS:
            return False
    return False


async def _tenants_rollup(pool) -> dict:
    """total/active/suspended từ PG omni_admin.tenant. onboarding = khe hở (Sub-slice B)."""
    if pool is None:
        gap = _gap("PG omni_admin không cấu hình (lab Redis-only) — tenant registry cần Postgres")
        return {"tenants": gap, "onboarding": gap}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM omni_admin.tenant GROUP BY status"
        )
    by_status = {r["status"]: int(r["n"]) for r in rows}
    total = sum(by_status.values())
    return {
        "tenants": _metric({
            "total": total,
            "active": by_status.get("active", 0),
            "suspended": by_status.get("suspended", 0),
        }),
        "onboarding": _gap("số tenant đang onboarding chưa expose — cần projection từ onboarding "
                            "mission runtime (mission có thật, chưa có read-model đếm)"),
    }


async def _component_health(redis, pool) -> dict:
    """Liveness phụ thuộc thật: Redis PING + Postgres SELECT 1. Không giả 'healthy'."""
    comps = []
    try:
        await redis.ping()
        comps.append({"name": "redis", "status": "ok"})
    except Exception as e:
        comps.append({"name": "redis", "status": "down", "detail": str(e)})
    if pool is None:
        comps.append({"name": "postgres", "status": "unavailable",
                      "detail": "OMNI_ADMIN_PG_DSN chưa cấu hình (lab Redis-only)"})
    else:
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            comps.append({"name": "postgres", "status": "ok"})
        except Exception as e:
            comps.append({"name": "postgres", "status": "down", "detail": str(e)})
    return _metric(comps)


async def build_provider_overview(redis, pool, trace, *, now: float) -> dict:
    """Payload Overview control-tower. Mỗi khoá là {available:true,value} hoặc {available:false,reason}."""
    tenants = await _tenants_rollup(pool)
    tr = await _trace_rollup(redis, trace, now)
    missions = await MissionStore(redis).list_all(limit=500)
    onboarding_count = sum(
        m.get("goal") == "onboard_tenant" and m.get("state") in {"in_progress", "blocked"}
        for m in missions
    )
    pending_questions = 0
    question_store_seen = False
    try:
        question_keys = await redis.keys(QUESTIONS_KEY.format(tenant_id="*"))
        question_store_seen = bool(question_keys)
        for key in question_keys:
            tenant_id = str(key).split("omni:aoip:questions:", 1)[-1]
            pending_questions += sum(
                q.get("status") == "PENDING" for q in await list_questions(redis, tenant_id)
            )
    except Exception as exc:  # noqa: BLE001 — preserve unavailable semantics
        logger.warning("overview: question projection unavailable err=%s", exc)
        pending_questions = -1
    if "error" in tr:  # Trace Spine không tới được → mọi metric trace = cùng khe hở
        trace_gap = tr["error"]
        tr = {k: trace_gap for k in
              ("active_incidents", "pending_approvals", "reconcile_required", "recent_activity")}
    return {
        "generated_at": now,
        "tenants": tenants["tenants"],
        "tenants_onboarding": (_metric(onboarding_count) if missions else tenants["onboarding"]),
        "agents": await _agent_fleet(redis, now),
        # Bỏ license_warnings & agent_version_drift: không có backend capability tương ứng
        # (governing rule 2026-07-01 — không placeholder metric cho domain chưa tồn tại backend).
        "missions": (_metric({
            "total": len(missions),
            "in_progress": sum(m.get("state") == "in_progress" for m in missions),
            "blocked": sum(m.get("state") == "blocked" for m in missions),
            "completed": sum(m.get("state") == "completed" for m in missions),
        }) if missions else _gap("chưa có Mission runtime nào được ghi nhận")),
        "active_incidents": tr["active_incidents"],
        "pending_approvals": tr["pending_approvals"],
        "pending_questions": (_metric(pending_questions) if question_store_seen and pending_questions >= 0 else _gap(
            "Question store (Redis) không truy cập được")),
        "reconcile_required": tr["reconcile_required"],
        "component_health": await _component_health(redis, pool),
        "recent_activity": tr["recent_activity"],
    }
