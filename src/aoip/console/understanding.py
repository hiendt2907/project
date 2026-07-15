"""Provider Understanding projection — System Twin/Competency/Unknowns.

Read-only projection for the operator portal. It folds existing AOIP runtime
stores into a tenant-level view; it never mutates twin, claims, questions, or
readiness state.
"""
from __future__ import annotations

import time
from typing import Any

from aoip.competency_matrix import (
    build_entity_competency_from_store,
    contradicted_facets,
    critical_unknowns,
    entity_coverage,
)
from aoip.question_lifecycle import list_questions, list_unknowns
from aoip.system_model_store import MODEL_KEY, load_contradictions, load_system_model

_MAX_FACTS = 80
_MAX_COMPETENCY_ENTITIES = 24


def _tenant_from_model_key(key: str) -> str:
    prefix = MODEL_KEY.format(tenant_id="")
    return str(key).replace(prefix, "", 1)


def _entity_type(entity_id: str) -> str | None:
    if entity_id.startswith("host:"):
        return "host"
    if entity_id.startswith("svc:"):
        return "service"
    return None


def _fact_view(f, *, now: float) -> dict[str, Any]:
    age = max(0, int(now - float(f.verified_time or f.observation_time or 0)))
    return {
        "subject": f.subject,
        "predicate": f.predicate,
        "object": f.obj,
        "confidence": f.confidence,
        "provenance": list(f.provenance),
        "observation_time": f.observation_time,
        "verified_time": f.verified_time,
        "freshness_seconds": age,
    }


async def _competency(redis: Any, tenant_id: str, entities: list[str],
                      *, now: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entity_id in entities[:_MAX_COMPETENCY_ENTITIES]:
        etype = _entity_type(entity_id)
        if etype is None:
            continue
        comp = await build_entity_competency_from_store(
            redis, tenant_id, entity_type=etype, entity_id=entity_id, now=now,
        )
        out.append({
            "entity_type": comp.entity_type,
            "entity_id": comp.entity_id,
            "coverage": entity_coverage(comp),
            "critical_unknowns": list(critical_unknowns(comp)),
            "contradicted_facets": list(contradicted_facets(comp)),
            "facets": {
                name: {
                    "state": value.state.value,
                    "confidence": value.confidence,
                    "last_verified_at": value.last_verified_at,
                    "evidence_refs": list(value.evidence_refs),
                }
                for name, value in comp.facets.items()
            },
        })
    return out


async def build_provider_understanding(redis: Any, *, now: float | None = None,
                                       tenant_id: str | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    keys = sorted(await redis.keys(MODEL_KEY.format(tenant_id="*")))
    tenants: list[dict[str, Any]] = []

    for key in keys:
        record_tenant_id = _tenant_from_model_key(str(key))
        if tenant_id is not None and record_tenant_id != tenant_id:
            continue
        model, revision = await load_system_model(redis, record_tenant_id)
        contradictions = await load_contradictions(redis, record_tenant_id)
        unknowns = await list_unknowns(redis, record_tenant_id)
        questions = await list_questions(redis, record_tenant_id)
        entities = sorted(model.known_nodes | model.entities | model.nodes_of_type("svc"))
        facts = sorted(model.facts, key=lambda f: (f.subject, f.predicate, f.obj))

        tenants.append({
            "tenant_id": record_tenant_id,
            "twin": {
                "revision": revision,
                "entity_count": len(entities),
                "fact_count": len(model.facts),
                "relationship_count": len(model.edges),
                "unknown_edge_targets": sorted(model.unknown_edge_targets),
            },
            "entities": entities,
            "relationships": [_fact_view(f, now=now) for f in model.edges],
            "facts": [_fact_view(f, now=now) for f in facts[:_MAX_FACTS]],
            "fact_limit": _MAX_FACTS,
            "contradictions": contradictions,
            "contradiction_count": len(contradictions),
            "unknowns": unknowns,
            "unknown_count": len(unknowns),
            "questions": questions,
            "question_count": len(questions),
            "competency": await _competency(redis, record_tenant_id, entities, now=now),
        })

    return {"generated_at": now, "tenant_count": len(tenants), "tenants": tenants}
