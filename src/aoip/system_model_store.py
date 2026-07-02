"""Persisted, versioned, per-tenant SystemModel store (Slice O1, Bước 6-7).

``aoip.system_model.SystemModel`` is a pure in-memory immutable value — this
module is the thin persistence adapter on top of it: Redis-backed, per-tenant
isolated, optimistic-versioned, with a bounded revision history. Process
memory is never the source of truth; every fold is read-modify-CAS-write.

Contradiction handling (Bước 5): discovery probes are periodic re-scans of the
SAME host, so a new Fact superseding an older one for the same (subject,
predicate) after a normal scan interval is a legitimate temporal replacement —
handled by ``SystemModel.fold`` itself (supersede by ``verified_time``). The
only case flagged as a genuine contradiction is two DIFFERENT sources
disagreeing about the same (subject, predicate) within the same short
observation window — that is ambiguous and must not be silently resolved by
picking a "winner"; both are kept (old fact stays in the model, the
conflicting new fact is preserved in the contradiction log instead of being
folded in) for human/-later resolution.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any, Sequence

from aoip.objects import Fact
from aoip.system_model import SystemModel

logger = logging.getLogger(__name__)

MODEL_KEY = "omni:aoip:system_model:{tenant_id}"
HISTORY_KEY = "omni:aoip:system_model_history:{tenant_id}"
CONTRADICTIONS_KEY = "omni:aoip:contradictions:{tenant_id}"

HISTORY_MAX = 200
CONTRADICTIONS_MAX = 200
_MAX_CAS_ATTEMPTS = 5
_CONTRADICTION_WINDOW_SEC = 60.0


class RevisionConflictError(Exception):
    """Raised when optimistic CAS could not land after ``_MAX_CAS_ATTEMPTS`` retries."""


def _serialize_fact(f: Fact) -> dict[str, Any]:
    return asdict(f)


def _deserialize_fact(d: dict[str, Any]) -> Fact:
    return Fact(
        subject=d["subject"],
        predicate=d["predicate"],
        obj=d["obj"],
        confidence=float(d["confidence"]),
        provenance=tuple(d.get("provenance") or ()),
        observation_time=float(d.get("observation_time", 0.0)),
        verified_time=float(d.get("verified_time", 0.0)),
    )


async def load_contradictions(redis: Any, tenant_id: str) -> list[dict[str, Any]]:
    """Read back the tenant's contradiction log (most-recent-first, as stored)."""
    raw = await redis.lrange(CONTRADICTIONS_KEY.format(tenant_id=tenant_id), 0, -1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:  # noqa: BLE001 — a malformed record must not break readers
            logger.warning("system_model_store: malformed contradiction record tenant=%s", tenant_id)
    return out


async def load_system_model(redis: Any, tenant_id: str) -> tuple[SystemModel, int]:
    """Reload persisted model + its revision. Empty model, revision 0 if absent."""
    raw = await redis.hgetall(MODEL_KEY.format(tenant_id=tenant_id))
    if not raw or "facts" not in raw:
        return SystemModel(scope=tenant_id), 0
    facts = tuple(_deserialize_fact(d) for d in json.loads(raw["facts"]))
    revision = int(raw.get("revision", 0))
    return SystemModel(scope=tenant_id, facts=facts), revision


def _split_contradictions(
    model: SystemModel, new_facts: Sequence[Fact],
) -> tuple[list[Fact], list[dict[str, Any]]]:
    """Partition ``new_facts`` into (safe-to-fold, contradiction records).

    Semantics (explicit, per review):
      - **Canonicalization**: subject/predicate are compared as exact strings.
        A subject always embeds its host (``host:web-01``), so two agents
        observing two DIFFERENT hosts never collide here — they simply never
        share a subject. Only same-subject, same-predicate candidates are
        compared at all (``model.facts_about(new.subject)`` scopes the lookup).
      - **Window**: ``_CONTRADICTION_WINDOW_SEC`` (60s) is measured on
        ``verified_time`` — the probe-reported observation time, not wall
        clock at persist time. It is a fixed constant for Slice O1 (no
        per-tenant override yet — a documented gap, not a silent default).
      - **Same source over time = supersession, not contradiction**: a
        candidate only becomes a contradiction when ``provenance`` differs
        from the existing fact's provenance (i.e. a genuinely different
        agent/probe run reported it) AND the two observations are close in
        time. The SAME source reporting a new value at ANY time gap is a
        normal temporal replacement (handled by ``_apply_supersession``),
        because a single sensor re-scanning itself cannot disagree with
        itself except by describing the current state changing.
      - A candidate contradicts an existing fact when: same (subject,
        predicate), different obj, both observed within
        ``_CONTRADICTION_WINDOW_SEC`` of each other, and provenance differs.
    """
    safe: list[Fact] = []
    contradictions: list[dict[str, Any]] = []
    for new in new_facts:
        existing = [
            f for f in model.facts_about(new.subject) if f.predicate == new.predicate
        ]
        conflict = next(
            (
                e for e in existing
                if e.obj != new.obj
                and e.provenance != new.provenance
                and abs(e.verified_time - new.verified_time) < _CONTRADICTION_WINDOW_SEC
            ),
            None,
        )
        if conflict is None:
            safe.append(new)
            continue
        contradictions.append(
            {
                "subject": new.subject,
                "predicate": new.predicate,
                "existing_obj": conflict.obj,
                "existing_provenance": list(conflict.provenance),
                "incoming_obj": new.obj,
                "incoming_provenance": list(new.provenance),
                "detected_at": time.time(),
            }
        )
    return safe, contradictions


def _apply_supersession(model: SystemModel, safe_facts: Sequence[Fact]) -> SystemModel:
    """Drop old facts a ``safe_fact`` clearly supersedes (same subject+predicate,
    different obj — a temporal replacement per Bước 5) before folding.

    ``SystemModel.fold`` only supersedes an EXACT triple match; distinct triples
    (different obj) are additive by design (assertional KB). For discovery
    probes that model a single current value (e.g. which service currently
    listens on a port), we want the newer value to replace the old one instead
    of both lingering forever — contradictions (ambiguous, different source,
    same window) are filtered out upstream and never reach here.
    """
    superseded: set[tuple[str, str, str]] = set()
    for new in safe_facts:
        for old in model.facts_about(new.subject):
            if old.predicate == new.predicate and old.obj != new.obj:
                superseded.add(old.triple)
    if not superseded:
        return model
    remaining = tuple(f for f in model.facts if f.triple not in superseded)
    return SystemModel(scope=model.scope, facts=remaining)


async def _append_contradictions(
    redis: Any, tenant_id: str, contradictions: list[dict[str, Any]],
) -> None:
    if not contradictions:
        return
    key = CONTRADICTIONS_KEY.format(tenant_id=tenant_id)
    for record in contradictions:
        await redis.lpush(key, json.dumps(record, ensure_ascii=False))
    await redis.ltrim(key, 0, CONTRADICTIONS_MAX - 1)


async def fold_and_persist(
    redis: Any, tenant_id: str, new_facts: Sequence[Fact], *, source: str,
) -> tuple[SystemModel, int, list[dict[str, Any]]]:
    """Read-modify-CAS-write ``new_facts`` into the tenant's persisted SystemModel.

    Returns (resulting model, new revision, contradiction records raised this
    call). Raises ``RevisionConflictError`` only if concurrent writers starve
    every retry — callers must treat that as a transient failure (do not lose
    the legacy discovery_doc write, which is independent of this path).
    """
    model_key = MODEL_KEY.format(tenant_id=tenant_id)
    history_key = HISTORY_KEY.format(tenant_id=tenant_id)

    for _attempt in range(_MAX_CAS_ATTEMPTS):
        model, revision = await load_system_model(redis, tenant_id)
        safe_facts, contradictions = _split_contradictions(model, new_facts)
        pruned = _apply_supersession(model, safe_facts) if safe_facts else model
        folded = pruned.fold(*safe_facts) if safe_facts else model
        if not contradictions and frozenset(folded.facts) == frozenset(model.facts):
            return model, revision, []

        payload = json.dumps([_serialize_fact(f) for f in folded.facts], ensure_ascii=False)
        new_revision = revision + 1

        async with redis.pipeline(transaction=True) as pipe:
            await pipe.watch(model_key)
            current_raw = await pipe.hget(model_key, "revision")
            current_revision = int(current_raw) if current_raw else 0
            if current_revision != revision:
                await pipe.unwatch()
                continue  # optimistic conflict — reload and retry
            pipe.multi()
            pipe.hset(
                model_key,
                mapping={
                    "facts": payload,
                    "revision": new_revision,
                    "updated_at": str(int(time.time())),
                },
            )
            pipe.lpush(
                history_key,
                json.dumps(
                    {
                        "revision": new_revision,
                        "triples_added": [list(f.triple) for f in safe_facts],
                        "source": source,
                        "ts": int(time.time()),
                    },
                    ensure_ascii=False,
                ),
            )
            pipe.ltrim(history_key, 0, HISTORY_MAX - 1)
            try:
                await pipe.execute()
            except Exception as exc:  # noqa: BLE001 — WATCH aborted by concurrent writer
                logger.debug("system_model_store: CAS retry tenant=%s err=%s", tenant_id, exc)
                continue

        await _append_contradictions(redis, tenant_id, contradictions)
        return folded, new_revision, contradictions

    raise RevisionConflictError(
        f"fold_and_persist: exhausted {_MAX_CAS_ATTEMPTS} CAS attempts tenant={tenant_id}"
    )
