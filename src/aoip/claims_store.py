"""Human-answer Claims — Slice O2B.

A Claim is what a human told Omni in response to a structured Question
(see ``aoip.question_lifecycle``). It is a distinct concept from a machine
``Fact``: a Claim is unverified until corroborated. It is stored separately
from ``SystemModel`` (never folded through ``system_model_store.fold_and_persist``)
specifically so a human answer can never silently supersede or out-race a
machine-observed Fact — ``aoip.competency_matrix`` reads both sources and
decides CLAIMED vs VERIFIED vs CONTRADICTED itself (Bước 5 priority rules).

Only one Claim is kept per (tenant, subject, predicate) — the latest answer
replaces the previous one (an answer supersedes an older answer to the same
question-shape; history lives in the Question/Answer log itself, see
``question_lifecycle.py``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

CLAIMS_KEY = "omni:aoip:claims:{tenant_id}"


@dataclass(frozen=True)
class ClaimRecord:
    subject: str
    predicate: str
    value: str
    answered_by: str
    answered_at: float
    question_id: str
    confidence: float = 0.6


def _claim_field(subject: str, predicate: str) -> str:
    return f"{subject}␟{predicate}"


async def put_claim(redis: Any, tenant_id: str, claim: ClaimRecord) -> None:
    key = CLAIMS_KEY.format(tenant_id=tenant_id)
    field = _claim_field(claim.subject, claim.predicate)
    await redis.hset(key, field, json.dumps(asdict(claim), ensure_ascii=False))


async def load_claims(redis: Any, tenant_id: str) -> list[ClaimRecord]:
    """All claims for a tenant (small set — one per subject/predicate pair)."""
    raw = await redis.hgetall(CLAIMS_KEY.format(tenant_id=tenant_id))
    out: list[ClaimRecord] = []
    for value in raw.values():
        try:
            out.append(ClaimRecord(**json.loads(value)))
        except Exception:  # noqa: BLE001 — a malformed record must not break readers
            logger.warning("claims_store: malformed claim record tenant=%s", tenant_id)
    return out


async def get_claim(redis: Any, tenant_id: str, subject: str, predicate: str) -> ClaimRecord | None:
    raw = await redis.hget(CLAIMS_KEY.format(tenant_id=tenant_id), _claim_field(subject, predicate))
    if not raw:
        return None
    try:
        return ClaimRecord(**json.loads(raw))
    except Exception:  # noqa: BLE001
        logger.warning("claims_store: malformed claim record tenant=%s subject=%s", tenant_id, subject)
        return None
