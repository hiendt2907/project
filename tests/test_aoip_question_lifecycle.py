"""Slice O2B: structured Unknown -> deduplicated Question -> human Answer -> Claim."""
from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest

from aoip.claims_store import get_claim
from aoip.competency_matrix import FacetState, build_entity_competency
from aoip.objects import Fact
from aoip.question_lifecycle import (
    QuestionStatus,
    UnknownStatus,
    compute_fingerprint,
    ensure_question_for_unknown,
    get_question,
    list_questions,
    list_unknowns,
    expire_stale_questions,
    submit_answer,
    sync_unknowns_from_competency,
)
from aoip.system_model import SystemModel


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _fact(subject: str, predicate: str, obj: str, *, ts: float = 1000.0) -> Fact:
    return Fact(
        subject=subject, predicate=predicate, obj=obj, confidence=0.9,
        provenance=("discovery:port_scan:tr1", "agent:a1"), observation_time=ts, verified_time=ts,
    )


def _service_comp(now: float = 2000.0):
    model = SystemModel(scope="acme", facts=(_fact("host:web-01", "runs_service", "payment-api"),))
    return build_entity_competency(model, [], entity_type="service", entity_id="svc:payment-api", now=now)


class TestFingerprint:
    def test_deterministic(self):
        a = compute_fingerprint("acme", "service", "svc:payment-api", "owner", "missing")
        b = compute_fingerprint("acme", "service", "svc:payment-api", "owner", "missing")
        assert a == b

    def test_distinct_per_tenant_entity_facet(self):
        a = compute_fingerprint("acme", "service", "svc:payment-api", "owner", "missing")
        b = compute_fingerprint("globex", "service", "svc:payment-api", "owner", "missing")
        c = compute_fingerprint("acme", "service", "svc:other", "owner", "missing")
        d = compute_fingerprint("acme", "service", "svc:payment-api", "sla", "missing")
        assert len({a, b, c, d}) == 4


class TestDedup:
    @pytest.mark.asyncio
    async def test_same_evidence_repeated_yields_one_open_unknown(self):
        r = _redis()
        comp = _service_comp()
        await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        await sync_unknowns_from_competency(r, "acme", comp, now=2000.0)
        await sync_unknowns_from_competency(r, "acme", comp, now=3000.0)
        unknowns = await list_unknowns(r, "acme")
        owner_unknowns = [u for u in unknowns if u["facet"] == "owner"]
        assert len(owner_unknowns) == 1
        assert owner_unknowns[0]["last_seen_at"] == 3000.0

    @pytest.mark.asyncio
    async def test_pending_question_not_recreated(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        q1 = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        assert q1 is not None
        assert q1["status"] == QuestionStatus.PENDING.value
        q2 = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1500.0)
        assert q2 is None  # already pending — no duplicate
        questions = await list_questions(r, "acme")
        assert len([q for q in questions if q["facet"] == "owner"]) == 1

    @pytest.mark.asyncio
    async def test_distinct_tenant_entity_facet_gets_distinct_questions(self):
        r = _redis()
        comp_a = _service_comp()
        touched_a = await sync_unknowns_from_competency(r, "acme", comp_a, now=1000.0)
        owner_a = next(u for u in touched_a if u["facet"] == "owner")
        await ensure_question_for_unknown(r, "acme", owner_a, now=1000.0)

        model_b = SystemModel(scope="globex", facts=(_fact("host:db-01", "runs_service", "billing"),))
        comp_b = build_entity_competency(model_b, [], entity_type="service", entity_id="svc:billing", now=1000.0)
        touched_b = await sync_unknowns_from_competency(r, "globex", comp_b, now=1000.0)
        owner_b = next(u for u in touched_b if u["facet"] == "owner")
        await ensure_question_for_unknown(r, "globex", owner_b, now=1000.0)

        assert len(await list_questions(r, "acme")) >= 1
        assert len(await list_questions(r, "globex")) >= 1
        assert owner_a["unknown_id"] != owner_b["unknown_id"]


class TestAnswerLifecycle:
    @pytest.mark.asyncio
    async def test_answer_creates_claim_with_human_provenance(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)

        answer = await submit_answer(
            r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0,
        )
        assert answer is not None
        assert answer["answered_by"] == "alice"

        claim = await get_claim(r, "acme", "svc:payment-api", "owned_by")
        assert claim is not None
        assert claim.value == "team-payments"
        assert claim.answered_by == "alice"

    @pytest.mark.asyncio
    async def test_answer_projects_facet_to_claimed_not_verified(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0)

        from aoip.claims_store import load_claims
        claims = await load_claims(r, "acme")
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "runs_service", "payment-api"),))
        comp2 = build_entity_competency(
            model, [], entity_type="service", entity_id="svc:payment-api", claims=claims, now=1200.0,
        )
        assert comp2.facet("owner").state == FacetState.CLAIMED

    @pytest.mark.asyncio
    async def test_matching_machine_fact_upgrades_claim_to_verified(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0)

        from aoip.claims_store import load_claims
        claims = await load_claims(r, "acme")
        model = SystemModel(
            scope="acme",
            facts=(
                _fact("host:web-01", "runs_service", "payment-api"),
                _fact("svc:payment-api", "owned_by", "team-payments", ts=1200.0),
            ),
        )
        comp2 = build_entity_competency(
            model, [], entity_type="service", entity_id="svc:payment-api", claims=claims, now=1300.0,
        )
        assert comp2.facet("owner").state == FacetState.VERIFIED

    @pytest.mark.asyncio
    async def test_conflicting_machine_fact_contradicts_claim(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0)

        from aoip.claims_store import load_claims
        claims = await load_claims(r, "acme")
        model = SystemModel(
            scope="acme",
            facts=(
                _fact("host:web-01", "runs_service", "payment-api"),
                _fact("svc:payment-api", "owned_by", "team-checkout", ts=1200.0),
            ),
        )
        comp2 = build_entity_competency(
            model, [], entity_type="service", entity_id="svc:payment-api", claims=claims, now=1300.0,
        )
        assert comp2.facet("owner").state == FacetState.CONTRADICTED

    @pytest.mark.asyncio
    async def test_answer_alone_never_becomes_verified(self):
        """No machine fact at all -> answer stays CLAIMED forever, never auto-VERIFIED."""
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0)

        from aoip.claims_store import load_claims
        claims = await load_claims(r, "acme")
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "runs_service", "payment-api"),))
        comp2 = build_entity_competency(
            model, [], entity_type="service", entity_id="svc:payment-api", claims=claims, now=99999999.0,
        )
        assert comp2.facet("owner").state in (FacetState.CLAIMED, FacetState.STALE)
        assert comp2.facet("owner").state != FacetState.VERIFIED

    @pytest.mark.asyncio
    async def test_cannot_answer_non_pending_question(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0)
        second = await submit_answer(r, "acme", question["question_id"], answered_by="bob", value="team-x", now=1200.0)
        assert second is None


class TestResolution:
    @pytest.mark.asyncio
    async def test_machine_evidence_resolves_pending_unknown_and_question(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)

        model = SystemModel(
            scope="acme",
            facts=(
                _fact("host:web-01", "runs_service", "payment-api"),
                _fact("svc:payment-api", "owned_by", "team-payments", ts=1200.0),
            ),
        )
        comp2 = build_entity_competency(model, [], entity_type="service", entity_id="svc:payment-api", now=1300.0)
        assert comp2.facet("owner").state == FacetState.VERIFIED
        await sync_unknowns_from_competency(r, "acme", comp2, now=1300.0)

        unknowns = await list_unknowns(r, "acme")
        owner_after = next(u for u in unknowns if u["facet"] == "owner")
        assert owner_after["status"] == UnknownStatus.RESOLVED.value
        question_after = await get_question(r, "acme", owner_unknown["unknown_id"])
        assert question_after["status"] == QuestionStatus.RESOLVED.value

    @pytest.mark.asyncio
    async def test_answered_question_does_not_reopen_on_repeated_same_evidence(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-payments", now=1100.0)

        # Repeated identical (still-UNKNOWN-from-machine-side) evidence must not re-ask.
        again = await sync_unknowns_from_competency(r, "acme", comp, now=1200.0)
        owner_again = next(u for u in again if u["facet"] == "owner")
        q_again = await ensure_question_for_unknown(r, "acme", owner_again, now=1200.0)
        assert q_again is None


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_b_cannot_answer_tenant_a_question(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)

        result = await submit_answer(
            r, "globex", question["question_id"], answered_by="mallory", value="team-x", now=1100.0,
        )
        assert result is None


class TestDataResidency:
    @pytest.mark.asyncio
    async def test_answer_value_bounded_length(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        huge = "x" * 5000
        answer = await submit_answer(r, "acme", question["question_id"], answered_by="alice", value=huge, now=1100.0)
        assert len(answer["value"]) <= 500


class TestExpiry:
    @pytest.mark.asyncio
    async def test_pending_question_past_expires_at_becomes_expired(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        assert question["expires_at"] == 1000.0 + 7 * 86400.0

        expired = await expire_stale_questions(r, "acme", now=1000.0 + 8 * 86400.0)
        assert expired >= 1

        q = await get_question(r, "acme", question["question_id"])
        assert q["status"] == QuestionStatus.EXPIRED.value
        unknowns = {u["unknown_id"]: u for u in await list_unknowns(r, "acme")}
        assert unknowns[question["unknown_id"]]["status"] == UnknownStatus.OPEN.value

    @pytest.mark.asyncio
    async def test_fresh_pending_question_not_expired(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)

        expired = await expire_stale_questions(r, "acme", now=2000.0)
        assert expired == 0
        q = await get_question(r, "acme", question["question_id"])
        assert q["status"] == QuestionStatus.PENDING.value

    @pytest.mark.asyncio
    async def test_answered_question_never_expired(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        question = await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await submit_answer(r, "acme", question["question_id"], answered_by="alice", value="team-a", now=1100.0)

        expired = await expire_stale_questions(r, "acme", now=1000.0 + 30 * 86400.0)
        assert expired == 0
        q = await get_question(r, "acme", question["question_id"])
        assert q["status"] == QuestionStatus.ANSWERED.value

    @pytest.mark.asyncio
    async def test_expired_question_can_be_reasked(self):
        r = _redis()
        comp = _service_comp()
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)
        await expire_stale_questions(r, "acme", now=1000.0 + 8 * 86400.0)

        unknowns = {u["unknown_id"]: u for u in await list_unknowns(r, "acme")}
        reopened = unknowns[owner_unknown["unknown_id"]]
        q2 = await ensure_question_for_unknown(r, "acme", reopened, now=1000.0 + 8 * 86400.0)
        assert q2 is not None
        assert q2["status"] == QuestionStatus.PENDING.value
