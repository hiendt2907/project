"""Structured Unknown -> deduplicated Question -> human Answer -> Claim
lifecycle (Slice O2B).

Distinct from the legacy ad-hoc gap detection in
``workers.onboarding_pipeline._detect_gaps_and_ask`` (kept running unchanged —
Bước 7 compatibility): that path fires a free-text Telegram question per probe
event with no persistence/dedup/answer-ingestion. This module is entity/facet
aware, deduplicates deterministically, and turns an answer into a Claim that
``aoip.competency_matrix`` can project (CLAIMED, then VERIFIED/CONTRADICTED
once machine evidence corroborates or disputes it).

An answer is NEVER treated as verified knowledge on its own — only
``competency_matrix`` decides VERIFIED, by cross-checking a matching machine
Fact. This module only ever writes CLAIMED-level state.

Full boundary/rationale versus the legacy path (field-by-field, why the two
do NOT violate ``INV_SINGLE_SOURCE_OF_TRUTH``, decision guide for future code):
``docs/architecture/QUESTION_PATH_BOUNDARY.md``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from aoip.claims_store import ClaimRecord, put_claim
from aoip.competency_matrix import FACET_PREDICATE, EntityCompetency, FacetState

logger = logging.getLogger(__name__)

UNKNOWNS_KEY = "omni:aoip:unknowns:{tenant_id}"
QUESTIONS_KEY = "omni:aoip:questions:{tenant_id}"
ANSWERS_KEY = "omni:aoip:answers:{tenant_id}"

DEFAULT_QUESTION_TTL_SEC = 7 * 86400.0
_CRITICAL_FACETS = frozenset({"owner", "monitoring", "sla"})


class UnknownStatus(str, Enum):
    OPEN = "OPEN"
    QUESTION_PENDING = "QUESTION_PENDING"
    CLAIMED = "CLAIMED"
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"
    RESOLVED = "RESOLVED"
    STALE = "STALE"


class QuestionStatus(str, Enum):
    PENDING = "PENDING"
    ANSWERED = "ANSWERED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


_TERMINAL_UNKNOWN_STATUSES = frozenset({UnknownStatus.RESOLVED.value})
_UNANSWERABLE_QUESTION_STATUSES = frozenset(
    {QuestionStatus.RESOLVED.value, QuestionStatus.EXPIRED.value, QuestionStatus.CANCELLED.value}
)


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    tenant_id: str
    entity_type: str
    entity_id: str
    facet: str
    reason: str  # "missing" | "contradicted"
    evidence_refs: tuple[str, ...]
    created_at: float
    last_seen_at: float
    status: str
    severity: str  # "high" | "medium" | "low"
    source: str = "competency_matrix"


@dataclass(frozen=True)
class Question:
    question_id: str
    unknown_id: str
    tenant_id: str
    entity_type: str
    entity_id: str
    facet: str
    question_type: str
    normalized_fingerprint: str
    text: str
    context_summary: str
    known_evidence: tuple[str, ...]
    created_at: float
    expires_at: float | None
    status: str
    asked_via: str
    target_role: str
    answer_id: str | None = None


@dataclass(frozen=True)
class Answer:
    answer_id: str
    question_id: str
    tenant_id: str
    answered_by: str
    answered_at: float
    value: str
    source_channel: str
    confidence: float = 0.6
    evidence_reference: str | None = None


def compute_fingerprint(tenant_id: str, entity_type: str, entity_id: str, facet: str, reason: str) -> str:
    """Deterministic dedup key — same (tenant, entity, facet, reason class)
    always yields the same fingerprint, independent of question wording."""
    raw = f"{tenant_id}␟{entity_type}␟{entity_id}␟{facet}␟{reason}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _severity_for(facet: str) -> str:
    return "high" if facet in _CRITICAL_FACETS else "medium"


def _reason_for(state: FacetState) -> str | None:
    if state == FacetState.CONTRADICTED:
        return "contradicted"
    if state == FacetState.UNKNOWN:
        return "missing"
    return None


async def _get(redis: Any, key: str, field: str) -> dict[str, Any] | None:
    raw = await redis.hget(key, field)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.warning("question_lifecycle: malformed record key=%s field=%s", key, field)
        return None


async def _put(redis: Any, key: str, field: str, record: dict[str, Any]) -> None:
    await redis.hset(key, field, json.dumps(record, ensure_ascii=False))


# ── Unknown lifecycle (Bước 2) ────────────────────────────────────────────

async def sync_unknowns_from_competency(
    redis: Any, tenant_id: str, competency: EntityCompetency, *, now: float | None = None,
) -> list[dict[str, Any]]:
    """Scan a projected EntityCompetency for UNKNOWN/CONTRADICTED facets and
    open (or refresh) a deduplicated Unknown record for each. Facets that are
    no longer UNKNOWN/CONTRADICTED (resolved by fresh evidence) auto-resolve
    their Unknown — Bước 6, "machine evidence resolves it", not "the question
    got answered"."""
    resolved_now = now if now is not None else time.time()
    key = UNKNOWNS_KEY.format(tenant_id=tenant_id)
    touched: list[dict[str, Any]] = []

    for facet_name, facet_value in competency.facets.items():
        reason = _reason_for(facet_value.state)
        fingerprint = compute_fingerprint(
            tenant_id, competency.entity_type, competency.entity_id, facet_name, reason or "resolved",
        )
        existing = await _get(redis, key, fingerprint)

        if reason is None:
            # Facet is now VERIFIED/CLAIMED/STALE/OBSERVED/NOT_APPLICABLE — any open
            # Unknown for it (under the "missing"/"contradicted" fingerprints) resolves.
            for candidate_reason in ("missing", "contradicted"):
                candidate_fp = compute_fingerprint(
                    tenant_id, competency.entity_type, competency.entity_id, facet_name, candidate_reason,
                )
                candidate = await _get(redis, key, candidate_fp)
                if candidate is not None and candidate["status"] not in _TERMINAL_UNKNOWN_STATUSES:
                    candidate["status"] = UnknownStatus.RESOLVED.value
                    candidate["last_seen_at"] = resolved_now
                    await _put(redis, key, candidate_fp, candidate)
                    await _resolve_pending_question(redis, tenant_id, candidate_fp, now=resolved_now)
            continue

        if existing is not None and existing["status"] not in _TERMINAL_UNKNOWN_STATUSES:
            existing["last_seen_at"] = resolved_now
            existing["evidence_refs"] = list(facet_value.evidence_refs)
            await _put(redis, key, fingerprint, existing)
            touched.append(existing)
            continue

        record = Unknown(
            unknown_id=fingerprint, tenant_id=tenant_id, entity_type=competency.entity_type,
            entity_id=competency.entity_id, facet=facet_name, reason=reason,
            evidence_refs=facet_value.evidence_refs, created_at=resolved_now, last_seen_at=resolved_now,
            status=UnknownStatus.OPEN.value, severity=_severity_for(facet_name),
        )
        await _put(redis, key, fingerprint, asdict(record))
        touched.append(asdict(record))

    return touched


async def _resolve_pending_question(redis: Any, tenant_id: str, unknown_id: str, *, now: float) -> None:
    q_key = QUESTIONS_KEY.format(tenant_id=tenant_id)
    question = await _get(redis, q_key, unknown_id)  # question_id == unknown_id (1:1 in this slice)
    if question is None or question["status"] in _UNANSWERABLE_QUESTION_STATUSES:
        return
    question["status"] = QuestionStatus.RESOLVED.value
    await _put(redis, q_key, unknown_id, question)


# ── Question lifecycle (Bước 3) ───────────────────────────────────────────

def _default_render(unknown: dict[str, Any]) -> str:
    facet = unknown["facet"]
    entity = unknown["entity_id"]
    if unknown["reason"] == "contradicted":
        return f"Omni thấy thông tin mâu thuẫn về '{facet}' của {entity} — bạn xác nhận giá trị đúng giúp không?"
    return f"Omni chưa biết '{facet}' của {entity} — bạn cung cấp giúp được không?"


async def ensure_question_for_unknown(
    redis: Any, tenant_id: str, unknown: dict[str, Any], *,
    render_text=_default_render, now: float | None = None,
    ttl_sec: float = DEFAULT_QUESTION_TTL_SEC, asked_via: str = "telegram", target_role: str = "tenant_admin",
) -> dict[str, Any] | None:
    """Create a PENDING question for this Unknown unless one is already
    pending, or a still-valid answer already exists (Bước 3: no re-asking)."""
    resolved_now = now if now is not None else time.time()
    q_key = QUESTIONS_KEY.format(tenant_id=tenant_id)
    question_id = unknown["unknown_id"]  # deterministic 1:1 — same dedup key
    existing = await _get(redis, q_key, question_id)

    if existing is not None:
        if existing["status"] == QuestionStatus.PENDING.value:
            return None  # already pending — do not create a duplicate
        if existing["status"] == QuestionStatus.ANSWERED.value:
            return None  # already answered and not yet re-opened as an Unknown
        # EXPIRED / CANCELLED / RESOLVED -> the Unknown is open again, fall through to re-ask.

    question = Question(
        question_id=question_id, unknown_id=unknown["unknown_id"], tenant_id=tenant_id,
        entity_type=unknown["entity_type"], entity_id=unknown["entity_id"], facet=unknown["facet"],
        question_type="fact_request" if unknown["reason"] == "missing" else "conflict_resolution",
        normalized_fingerprint=question_id, text=render_text(unknown),
        context_summary=f"reason={unknown['reason']} severity={unknown['severity']}",
        known_evidence=tuple(unknown.get("evidence_refs") or ()),
        created_at=resolved_now, expires_at=resolved_now + ttl_sec, status=QuestionStatus.PENDING.value,
        asked_via=asked_via, target_role=target_role,
    )
    await _put(redis, q_key, question_id, asdict(question))

    u_key = UNKNOWNS_KEY.format(tenant_id=tenant_id)
    unknown_record = dict(unknown)
    unknown_record["status"] = UnknownStatus.QUESTION_PENDING.value
    await _put(redis, u_key, unknown["unknown_id"], unknown_record)
    return asdict(question)


def render_telegram_text(question: dict[str, Any]) -> str:
    """Bước 7: existing Telegram send path only needs plain text — this keeps
    structured questions renderable through the same channel without change."""
    return question["text"]


async def expire_stale_questions(redis: Any, tenant_id: str, *, now: float | None = None) -> int:
    """Chuyển PENDING question đã quá ``expires_at`` sang EXPIRED và mở lại
    Unknown tương ứng (QUESTION_PENDING → OPEN) để paced caller có thể re-ask.

    Không có bước này, question PENDING tích lũy vô hạn: ``expires_at`` được
    ghi lúc tạo nhưng không nơi nào thực thi nó. Trả về số question đã expire.
    """
    resolved_now = now if now is not None else time.time()
    q_key = QUESTIONS_KEY.format(tenant_id=tenant_id)
    u_key = UNKNOWNS_KEY.format(tenant_id=tenant_id)
    expired = 0

    raw = await redis.hgetall(q_key)
    for field, value in raw.items():
        try:
            question = json.loads(value)
        except Exception:  # noqa: BLE001
            logger.warning("question_lifecycle: malformed question key=%s field=%s", q_key, field)
            continue
        if question.get("status") != QuestionStatus.PENDING.value:
            continue
        expires_at = question.get("expires_at")
        if expires_at is None or expires_at >= resolved_now:
            continue

        question["status"] = QuestionStatus.EXPIRED.value
        await _put(redis, q_key, field, question)
        expired += 1

        unknown = await _get(redis, u_key, question["unknown_id"])
        if unknown is not None and unknown["status"] == UnknownStatus.QUESTION_PENDING.value:
            unknown["status"] = UnknownStatus.OPEN.value
            unknown["last_seen_at"] = resolved_now
            await _put(redis, u_key, question["unknown_id"], unknown)

    if expired:
        logger.info("question_lifecycle: expired %d stale questions tenant=%s", expired, tenant_id)
    return expired


# ── Answer-as-Claim (Bước 4) ──────────────────────────────────────────────

async def submit_answer(
    redis: Any, tenant_id: str, question_id: str, *,
    answered_by: str, value: str, source_channel: str = "telegram",
    confidence: float = 0.6, evidence_reference: str | None = None, now: float | None = None,
) -> dict[str, Any] | None:
    """Ingest a human answer. Returns None if the question does not exist, is
    not PENDING, or does not belong to this tenant (tenant isolation is
    enforced by the Redis key namespace itself — a tenant can only look up
    questions under its own ``QUESTIONS_KEY``)."""
    resolved_now = now if now is not None else time.time()
    q_key = QUESTIONS_KEY.format(tenant_id=tenant_id)
    question = await _get(redis, q_key, question_id)
    if question is None or question["status"] != QuestionStatus.PENDING.value:
        return None

    safe_value = str(value)[:500]  # data residency: bounded, structured value — no raw doc dumps
    answer_id = hashlib.sha256(f"{tenant_id}:{question_id}:{resolved_now}".encode("utf-8")).hexdigest()[:20]
    answer = Answer(
        answer_id=answer_id, question_id=question_id, tenant_id=tenant_id, answered_by=answered_by,
        answered_at=resolved_now, value=safe_value, source_channel=source_channel,
        confidence=confidence, evidence_reference=evidence_reference,
    )
    await _put(redis, ANSWERS_KEY.format(tenant_id=tenant_id), answer_id, asdict(answer))

    question["status"] = QuestionStatus.ANSWERED.value
    question["answer_id"] = answer_id
    await _put(redis, q_key, question_id, question)

    u_key = UNKNOWNS_KEY.format(tenant_id=tenant_id)
    unknown = await _get(redis, u_key, question["unknown_id"])
    if unknown is not None:
        unknown["status"] = UnknownStatus.CLAIMED.value
        unknown["last_seen_at"] = resolved_now
        await _put(redis, u_key, question["unknown_id"], unknown)

    predicate = FACET_PREDICATE.get(question["facet"])
    if predicate is not None:
        claim = ClaimRecord(
            subject=question["entity_id"], predicate=predicate, value=safe_value,
            answered_by=answered_by, answered_at=resolved_now, question_id=question_id,
            confidence=confidence,
        )
        await put_claim(redis, tenant_id, claim)
    else:
        logger.info(
            "question_lifecycle: facet=%s has no claim predicate mapping (machine-only facet) — "
            "answer stored, not projected into competency matrix", question["facet"],
        )

    return asdict(answer)


# ── Read-only queries (Bước 8) ─────────────────────────────────────────────

async def list_unknowns(redis: Any, tenant_id: str) -> list[dict[str, Any]]:
    raw = await redis.hgetall(UNKNOWNS_KEY.format(tenant_id=tenant_id))
    return [json.loads(v) for v in raw.values()]


async def list_questions(redis: Any, tenant_id: str) -> list[dict[str, Any]]:
    raw = await redis.hgetall(QUESTIONS_KEY.format(tenant_id=tenant_id))
    return [json.loads(v) for v in raw.values()]


async def get_question(redis: Any, tenant_id: str, question_id: str) -> dict[str, Any] | None:
    return await _get(redis, QUESTIONS_KEY.format(tenant_id=tenant_id), question_id)
