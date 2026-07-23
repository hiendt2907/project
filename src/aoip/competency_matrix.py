"""Entity Competency Matrix — Slice O2A.

Answers "how much does Omni actually understand about entity X" as a facet
grid instead of a pile of disconnected Fact rows. This is a PURE, DERIVED
PROJECTION (INV_DERIVED_NEVER_PERSIST, same principle as ``SystemModel`` over
``Fact``): it reads the persisted ``SystemModel`` + contradiction log and
computes the matrix on demand — no parallel graph/model is created or
persisted. Same inputs always produce the same matrix (deterministic,
reconstructable).

Scope (Slice O2A): Host and Service entities only. Facets with no supporting
probe yet (owner, business_capability, upstream, downstream, monitoring,
logging, runbook, sla for Host — most of these for Service too) are honestly
reported UNKNOWN rather than guessed. No LLM is involved anywhere in this
module — CLAIMED never gets silently promoted to VERIFIED; state is decided
by deterministic evidence-confidence rules only.

Confidence-axis boundary (Phase 3, ``docs/architecture/
CONFIDENCE_AXES_BOUNDARY.md``): ``FacetState``/``FacetValue`` answer "how much
do we know about facet Y of entity X, accumulated over time" — long-lived
epistemic state, recomputed fresh from persisted ``SystemModel``/Claims on
every call, never persisted itself. This is a DIFFERENT axis from
``aoip.verification.VerificationResult``, which answers "did THIS mutation
attempt reach its expected_state, right now" — a one-shot, transient,
per-``RecoveryOutcome`` contract that is only ever audit-logged, never
recomputed as "current state". Do not conflate the two just because both
carry ``evidence_refs``/``confidence`` fields — a ``FacetState.VERIFIED`` (≥2
corroborating Facts over time, see ``_identity_facet``) is not the same claim
as a ``VerificationResult.status == PASS`` (one probe check just now). See the
boundary doc for the full field-by-field comparison and decision table.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from aoip.claims_store import ClaimRecord, load_claims
from aoip.objects import Fact
from aoip.system_model import SystemModel
from aoip.system_model_store import load_contradictions, load_system_model


class FacetState(str, Enum):
    UNKNOWN = "UNKNOWN"
    OBSERVED = "OBSERVED"
    CLAIMED = "CLAIMED"
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"
    STALE = "STALE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# Union of every facet this slice knows about. Which ones apply to a given
# entity_type is decided by ENTITY_APPLICABLE_FACETS below — facets outside
# that set are NOT_APPLICABLE for that type (e.g. a Host has no "sla").
FACET_SCHEMA: tuple[str, ...] = (
    "identity", "host", "runtime_state", "process", "listening_ports",
    "owner", "business_capability", "upstream", "downstream",
    "monitoring", "logging", "runbook", "sla",
)

ENTITY_APPLICABLE_FACETS: dict[str, frozenset[str]] = {
    "service": frozenset(FACET_SCHEMA),
    # NOT_APPLICABLE means "this facet has no meaning for this entity type" — reserved
    # for genuinely service-relational concepts (business_capability/upstream/downstream
    # describe a service's place in an app graph, not a host's). owner/runbook/monitoring/
    # logging DO make operational sense for a host (who owns it, how to page it) — those
    # are UNKNOWN-until-observed for Host, never NOT_APPLICABLE.
    "host": frozenset({
        "identity", "runtime_state", "process", "listening_ports",
        "monitoring", "logging", "owner", "runbook",
    }),
}

# Facets with no dedicated probe (Slice O1/O2A) — only ever populated via a human Claim
# (Slice O2B, see claims_store.py) until a future collector observes them directly.
FACET_PREDICATE: dict[str, str] = {
    "owner": "owned_by",
    "business_capability": "serves_capability",
    "monitoring": "monitored_by",
    "logging": "logged_by",
    "runbook": "has_runbook",
    "sla": "has_sla",
}

DEFAULT_FRESHNESS_SEC = 24 * 3600.0
VERIFIED_CONFIDENCE_MIN = 0.8


@dataclass(frozen=True)
class FacetValue:
    state: FacetState
    value: Any = None
    evidence_refs: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    confidence: float = 0.0
    last_observed_at: float | None = None
    last_verified_at: float | None = None


@dataclass(frozen=True)
class EntityCompetency:
    entity_type: str
    entity_id: str
    facets: dict[str, FacetValue] = field(default_factory=dict)

    def facet(self, name: str) -> FacetValue:
        return self.facets.get(name, FacetValue(state=FacetState.UNKNOWN))


def _source_types(provenance: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for p in provenance:
        kind = p.split(":", 1)[0] if p else "unknown"
        if kind not in seen:
            seen.append(kind)
    return tuple(seen)


def _fresh(verified_time: float, *, now: float, freshness_sec: float) -> bool:
    return (now - verified_time) <= freshness_sec


def _single_facet(
    facts: Sequence[Fact], *, now: float, freshness_sec: float,
    value_of=lambda f: f.obj,
) -> FacetValue:
    """Single-valued facet (e.g. which host runs a service): pick the most
    recently verified fact among the currently-fresh ones (or the freshest
    stale one if none are fresh — reported as STALE, not dropped)."""
    if not facts:
        return FacetValue(state=FacetState.UNKNOWN)
    fresh_facts = [f for f in facts if _fresh(f.verified_time, now=now, freshness_sec=freshness_sec)]
    if not fresh_facts:
        stalest_best = max(facts, key=lambda f: f.verified_time)
        return FacetValue(
            state=FacetState.STALE,
            value=value_of(stalest_best),
            evidence_refs=stalest_best.provenance,
            source_types=_source_types(stalest_best.provenance),
            confidence=stalest_best.confidence,
            last_observed_at=stalest_best.observation_time,
            last_verified_at=stalest_best.verified_time,
        )
    best = max(fresh_facts, key=lambda f: f.verified_time)
    state = FacetState.CLAIMED if any(p.startswith("human:") for p in best.provenance) else (
        FacetState.VERIFIED if best.confidence >= VERIFIED_CONFIDENCE_MIN else FacetState.OBSERVED
    )
    return FacetValue(
        state=state,
        value=value_of(best),
        evidence_refs=best.provenance,
        source_types=_source_types(best.provenance),
        confidence=best.confidence,
        last_observed_at=best.observation_time,
        last_verified_at=best.verified_time,
    )


def _multi_facet(facts: Sequence[Fact], *, now: float, freshness_sec: float) -> FacetValue:
    """Multi-valued facet (e.g. all listening ports on a host)."""
    if not facts:
        return FacetValue(state=FacetState.UNKNOWN)
    fresh_facts = [f for f in facts if _fresh(f.verified_time, now=now, freshness_sec=freshness_sec)]
    active = fresh_facts or facts
    values = tuple(sorted({f.obj for f in active}))
    evidence = tuple(sorted({p for f in active for p in f.provenance}))
    best_confidence = max(f.confidence for f in active)
    if not fresh_facts:
        state = FacetState.STALE
    elif best_confidence >= VERIFIED_CONFIDENCE_MIN:
        state = FacetState.VERIFIED
    else:
        state = FacetState.OBSERVED
    return FacetValue(
        state=state,
        value=values,
        evidence_refs=evidence,
        source_types=_source_types(evidence),
        confidence=best_confidence,
        last_observed_at=max(f.observation_time for f in active),
        last_verified_at=max(f.verified_time for f in active),
    )


def _contradiction_facet(record: dict[str, Any]) -> FacetValue:
    evidence = tuple(record.get("existing_provenance") or ()) + tuple(record.get("incoming_provenance") or ())
    return FacetValue(
        state=FacetState.CONTRADICTED,
        value=None,
        evidence_refs=evidence,
        source_types=_source_types(evidence),
        confidence=0.0,
        last_observed_at=record.get("detected_at"),
        last_verified_at=None,
    )


def _contradiction_index(contradictions: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Latest contradiction record per (subject, predicate) — deterministic:
    contradictions are stored most-recent-first (see system_model_store.
    _append_contradictions uses LPUSH), so the first match per key wins."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in contradictions:
        key = (rec.get("subject", ""), rec.get("predicate", ""))
        if key not in index:
            index[key] = rec
    return index


def _evidence_facts_for_entity(model: SystemModel, entity_type: str, entity_id: str) -> tuple[Fact, ...]:
    """Facts that constitute evidence this entity was actually observed.

    A Host is always the *subject* of its facts. A Service, in the current
    O1 projector, only ever appears as the *object* of a ``runs_service``
    fact (hosts are the subject) — so "evidence for this service" means
    facts pointing AT it, not facts about a ``svc:`` subject that doesn't
    exist yet.
    """
    if entity_type == "service":
        name = entity_id.split(":", 1)[1] if ":" in entity_id else entity_id
        return tuple(f for f in model.facts if f.predicate == "runs_service" and f.obj == name)
    return model.facts_about(entity_id)


def _identity_facet(model: SystemModel, entity_type: str, entity_id: str) -> FacetValue:
    """VERIFIED requires corroboration (>=2 distinct facts or >=2 distinct
    source types) — a single probe mentioning a name once is only OBSERVED,
    not a confirmed identity (Bước review risk #1)."""
    if entity_id not in model.known_nodes:
        return FacetValue(state=FacetState.UNKNOWN)
    evidence = _evidence_facts_for_entity(model, entity_type, entity_id)
    if not evidence:
        return FacetValue(state=FacetState.OBSERVED, value=entity_id, confidence=0.5)
    evidence_refs = tuple(sorted({p for f in evidence for p in f.provenance}))
    source_types = _source_types(evidence_refs)
    # A single Fact's own provenance tuple typically has >=2 entries by construction
    # (e.g. "discovery:probe:trace" + "agent:id") — that is NOT corroboration, it's
    # one observation described two ways. Corroboration requires >=2 distinct Facts
    # (i.e. the entity was actually observed more than once / via more than one probe).
    corroborated = len(evidence) >= 2
    state = FacetState.VERIFIED if corroborated else FacetState.OBSERVED
    confidence = max((f.confidence for f in evidence), default=0.5)
    return FacetValue(
        state=state, value=entity_id, evidence_refs=evidence_refs, source_types=source_types,
        confidence=confidence if corroborated else min(confidence, 0.7),
        last_observed_at=max(f.observation_time for f in evidence),
        last_verified_at=max(f.verified_time for f in evidence),
    )


def _claim_index(claims: Sequence[ClaimRecord]) -> dict[tuple[str, str], ClaimRecord]:
    """Latest claim per (subject, predicate). Callers pass claims already
    de-duplicated by ``claims_store`` (one per subject/predicate), this just
    guards against a caller passing raw duplicates."""
    index: dict[tuple[str, str], ClaimRecord] = {}
    for c in claims:
        key = (c.subject, c.predicate)
        prev = index.get(key)
        if prev is None or c.answered_at >= prev.answered_at:
            index[key] = c
    return index


CLAIM_FRESHNESS_SEC = 180 * 86400.0  # human answers age slower than machine probes (~6 months)


def _claimable_facet(
    model: SystemModel,
    contradiction_index: dict[tuple[str, str], dict[str, Any]],
    claim_index: dict[tuple[str, str], ClaimRecord],
    entity_id: str,
    facet: str,
    *,
    now: float,
) -> FacetValue:
    """Facets with no machine collector yet (owner/business_capability/monitoring/
    logging/runbook/sla) — populated only via a human Claim until corroborated.

    Priority (Bước 5): CONTRADICTED > VERIFIED > CLAIMED > OBSERVED > STALE/UNKNOWN.
    """
    predicate = FACET_PREDICATE[facet]
    key = (entity_id, predicate)
    if key in contradiction_index:  # machine-vs-machine conflict already flagged in O1
        return _contradiction_facet(contradiction_index[key])

    machine_facts = tuple(f for f in model.facts if f.subject == entity_id and f.predicate == predicate)
    claim = claim_index.get(key)

    if claim is not None and machine_facts:
        best_machine = max(machine_facts, key=lambda f: f.verified_time)
        evidence = best_machine.provenance + (f"human:{claim.answered_by}", f"question:{claim.question_id}")
        if best_machine.obj == claim.value:
            return FacetValue(
                state=FacetState.VERIFIED, value=claim.value, evidence_refs=evidence,
                source_types=_source_types(evidence), confidence=max(best_machine.confidence, claim.confidence),
                last_observed_at=best_machine.observation_time, last_verified_at=best_machine.verified_time,
            )
        return FacetValue(
            state=FacetState.CONTRADICTED, evidence_refs=evidence, source_types=_source_types(evidence),
        )

    if machine_facts:
        return _single_facet(machine_facts, now=now, freshness_sec=DEFAULT_FRESHNESS_SEC)

    if claim is None:
        return FacetValue(state=FacetState.UNKNOWN)

    evidence = (f"human:{claim.answered_by}", f"question:{claim.question_id}")
    if (now - claim.answered_at) > CLAIM_FRESHNESS_SEC:
        return FacetValue(
            state=FacetState.STALE, value=claim.value, evidence_refs=evidence, source_types=("human",),
            confidence=claim.confidence, last_observed_at=claim.answered_at,
        )
    return FacetValue(
        state=FacetState.CLAIMED, value=claim.value, evidence_refs=evidence, source_types=("human",),
        confidence=claim.confidence, last_observed_at=claim.answered_at,
    )


def _host_facet(
    model: SystemModel, contradiction_index: dict[tuple[str, str], dict[str, Any]],
    host_id: str, facet: str, *, now: float, freshness_sec: float,
) -> FacetValue:
    if facet == "runtime_state":
        facts = model.facts_about(host_id)
        return _single_facet(facts, now=now, freshness_sec=freshness_sec, value_of=lambda f: "reachable") \
            if facts else FacetValue(state=FacetState.UNKNOWN)
    if facet == "process":
        key = (host_id, "runs_process")
        if key in contradiction_index:
            return _contradiction_facet(contradiction_index[key])
        facts = tuple(f for f in model.facts_about(host_id) if f.predicate == "runs_process")
        return _multi_facet(facts, now=now, freshness_sec=freshness_sec)
    if facet == "listening_ports":
        key = (host_id, "exposes_port")
        if key in contradiction_index:
            return _contradiction_facet(contradiction_index[key])
        facts = tuple(f for f in model.facts_about(host_id) if f.predicate == "exposes_port")
        return _multi_facet(facts, now=now, freshness_sec=freshness_sec)
    return FacetValue(state=FacetState.UNKNOWN)


def _service_facet(
    model: SystemModel, contradiction_index: dict[tuple[str, str], dict[str, Any]],
    service_id: str, facet: str, *, now: float, freshness_sec: float,
) -> FacetValue:
    service_name = service_id.split(":", 1)[1] if ":" in service_id else service_id
    candidates = tuple(f for f in model.facts if f.predicate == "runs_service" and f.obj == service_name)
    if facet == "host":
        fresh = [f for f in candidates if _fresh(f.verified_time, now=now, freshness_sec=freshness_sec)]
        active = fresh or candidates
        if not candidates:
            return FacetValue(state=FacetState.UNKNOWN)
        if len({f.subject for f in active}) > 1:
            evidence = tuple(sorted({p for f in active for p in f.provenance}))
            return FacetValue(
                state=FacetState.CONTRADICTED,
                evidence_refs=evidence,
                source_types=_source_types(evidence),
            )
        return _single_facet(candidates, now=now, freshness_sec=freshness_sec, value_of=lambda f: f.subject)
    if facet == "runtime_state":
        return _single_facet(candidates, now=now, freshness_sec=freshness_sec, value_of=lambda f: "running")
    return FacetValue(state=FacetState.UNKNOWN)


def build_entity_competency(
    model: SystemModel,
    contradictions: Sequence[dict[str, Any]],
    *,
    entity_type: str,
    entity_id: str,
    claims: Sequence[ClaimRecord] = (),
    now: float | None = None,
    freshness_sec: float = DEFAULT_FRESHNESS_SEC,
) -> EntityCompetency:
    """Deterministic projection: same (model, contradictions, claims, now,
    freshness_sec) always yields the same EntityCompetency. Never calls an
    LLM; never promotes CLAIMED to VERIFIED on its own — only a matching
    machine Fact does that (see ``_claimable_facet``)."""
    resolved_now = now if now is not None else time.time()
    applicable = ENTITY_APPLICABLE_FACETS.get(entity_type, frozenset())
    contradiction_index = _contradiction_index(contradictions)
    claim_index = _claim_index(claims)

    facets: dict[str, FacetValue] = {}
    for facet in FACET_SCHEMA:
        if facet not in applicable:
            facets[facet] = FacetValue(state=FacetState.NOT_APPLICABLE)
            continue
        if facet == "identity":
            facets[facet] = _identity_facet(model, entity_type, entity_id)
            continue
        if facet in FACET_PREDICATE:
            facets[facet] = _claimable_facet(
                model, contradiction_index, claim_index, entity_id, facet, now=resolved_now,
            )
            continue
        if entity_type == "host":
            facets[facet] = _host_facet(
                model, contradiction_index, entity_id, facet, now=resolved_now, freshness_sec=freshness_sec,
            )
        elif entity_type == "service":
            facets[facet] = _service_facet(
                model, contradiction_index, entity_id, facet, now=resolved_now, freshness_sec=freshness_sec,
            )
        else:
            facets[facet] = FacetValue(state=FacetState.UNKNOWN)

    return EntityCompetency(entity_type=entity_type, entity_id=entity_id, facets=facets)


async def build_entity_competency_from_store(
    redis: Any, tenant_id: str, *, entity_type: str, entity_id: str,
    now: float | None = None, freshness_sec: float = DEFAULT_FRESHNESS_SEC,
) -> EntityCompetency:
    """Convenience wrapper: load the tenant's persisted twin + contradiction
    log + claims, then project. Read-only — never mutates the store."""
    model, _revision = await load_system_model(redis, tenant_id)
    contradictions = await load_contradictions(redis, tenant_id)
    claims = await load_claims(redis, tenant_id)
    return build_entity_competency(
        model, contradictions, entity_type=entity_type, entity_id=entity_id,
        claims=claims, now=now, freshness_sec=freshness_sec,
    )


# ── Query API (Bước 12) ───────────────────────────────────────────────────

def entity_coverage(comp: EntityCompetency) -> dict[str, Any]:
    """% of applicable facets that are anything other than UNKNOWN."""
    counts: dict[str, int] = {}
    for fv in comp.facets.values():
        counts[fv.state.value] = counts.get(fv.state.value, 0) + 1
    not_applicable = counts.get(FacetState.NOT_APPLICABLE.value, 0)
    unknown = counts.get(FacetState.UNKNOWN.value, 0)
    applicable_total = len(comp.facets) - not_applicable
    known = applicable_total - unknown
    coverage_pct = round(100.0 * known / applicable_total, 2) if applicable_total else 0.0
    return {
        "entity_id": comp.entity_id,
        "entity_type": comp.entity_type,
        "coverage_pct": coverage_pct,
        "state_counts": counts,
    }


def critical_unknowns(
    comp: EntityCompetency, *, critical_facets: tuple[str, ...] = ("owner", "monitoring", "sla"),
) -> tuple[str, ...]:
    return tuple(f for f in critical_facets if comp.facet(f).state == FacetState.UNKNOWN)


def contradicted_facets(comp: EntityCompetency) -> tuple[str, ...]:
    return tuple(f for f, v in comp.facets.items() if v.state == FacetState.CONTRADICTED)
