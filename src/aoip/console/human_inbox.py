"""Provider Human Inbox — questions/answers from AOIP unknown lifecycle."""
from __future__ import annotations

import time
from typing import Any

from aoip.competency_matrix import FACET_PREDICATE
from aoip.question_lifecycle import (
    UNKNOWNS_KEY,
    ensure_question_for_unknown,
    expire_stale_questions,
    list_questions,
    list_unknowns,
)

_MAX_QUESTIONS_TO_OPEN = 50
_CLAIMABLE_FACETS = frozenset(FACET_PREDICATE)


def _tenant_from_unknowns_key(key: str) -> str:
    prefix = UNKNOWNS_KEY.format(tenant_id="")
    return str(key).replace(prefix, "", 1)


async def _ensure_questions(redis: Any, tenant_id: str, unknowns: list[dict[str, Any]],
                            *, now: float) -> None:
    opened = 0
    sorted_unknowns = sorted(
        unknowns,
        key=lambda u: (
            u.get("facet") not in _CLAIMABLE_FACETS,
            u.get("severity") != "high",
            u.get("created_at", 0),
        ),
    )
    for unknown in sorted_unknowns:
        if opened >= _MAX_QUESTIONS_TO_OPEN:
            break
        if unknown.get("status") in {"OPEN", "QUESTION_PENDING"}:
            created = await ensure_question_for_unknown(
                redis, tenant_id, unknown, now=now, asked_via="provider_portal",
                target_role="operator",
            )
            if created is not None:
                opened += 1


async def build_provider_human_inbox(redis: Any, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    keys = sorted(await redis.keys(UNKNOWNS_KEY.format(tenant_id="*")))
    tenants: list[dict[str, Any]] = []
    total_unknowns = 0
    total_pending = 0

    for key in keys:
        tenant_id = _tenant_from_unknowns_key(str(key))
        await expire_stale_questions(redis, tenant_id, now=now)
        unknowns = await list_unknowns(redis, tenant_id)
        await _ensure_questions(redis, tenant_id, unknowns, now=now)
        questions = []
        for q in await list_questions(redis, tenant_id):
            q["can_create_claim"] = q.get("facet") in _CLAIMABLE_FACETS
            questions.append(q)
        questions = sorted(
            questions,
            key=lambda q: (
                q.get("status") != "PENDING",
                not q.get("can_create_claim", False),
                q.get("created_at", 0),
            ),
        )
        total_unknowns += len(unknowns)
        total_pending += sum(1 for q in questions if q.get("status") == "PENDING")
        tenants.append({
            "tenant_id": tenant_id,
            "unknown_count": len(unknowns),
            "question_count": len(questions),
            "pending_questions": sum(1 for q in questions if q.get("status") == "PENDING"),
            "questions": questions,
            "unknowns": unknowns[:100],
        })

    return {
        "generated_at": now,
        "summary": {
            "tenants": len(tenants),
            "unknowns": total_unknowns,
            "pending_questions": total_pending,
        },
        "tenants": tenants,
    }
