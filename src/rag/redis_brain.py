"""Redis Stack as Omni's *second brain* — a multi-turn RAG reasoning loop.

Motivation
----------
The classic ``recall_playbook_advisory`` does a single one-shot vector lookup.
That treats every query independently and never builds up an understanding of one
*complete* alert. This module instead runs a **multi-turn question→answer loop
over Redis Stack within ONE session per alert** (keyed by ``trace_id``), mirroring
how Omni talks to the LLM — but backed by the vector store, which is much faster
and cheaper than an LLM round-trip.

Each turn:
  1. queries one or more vector collections,
  2. accumulates the newly-retrieved knowledge into the *session context*
     (carried forward — turns are NOT independent),
  3. refines the next query from what it has learned so far,
  4. stops when it is confident (strong hit), stops gaining new knowledge, or
     hits ``max_turns``.

The result is either a high-confidence answer (used to short-circuit / pre-seed
the LLM) or a rich accumulated context that is injected into the LLM prompt — so
the LLM starts from Redis's synthesized understanding instead of a single snippet.

Session state lives at ``omni:brain:session:{trace}`` (TTL) so the whole loop is
inspectable and the UI can render the second brain's turns like an LLM session.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from rag.redis_vector_store import (
    COLLECTION_ACTION_EXPERIENCE,
    COLLECTION_SOP,
    COLLECTION_SRE_KNOWLEDGE,
)

logger = logging.getLogger(__name__)

SESSION_KEY = "omni:brain:session:{trace}"
SESSION_TTL_SEC = 3600

DEFAULT_MAX_TURNS = 3
CONFIDENCE_THRESHOLD = 0.85   # strong hit → confident, can short-circuit the LLM
MIN_HIT_SCORE = 0.55          # below this a hit is noise, ignored
PER_TURN_LIMIT = 4            # hits pulled per collection per turn
MAX_CONTEXT_SNIPPETS = 12     # cap accumulated context size

# Collections the second brain consults, in priority order. action_experience
# (verified past remediations) first, then SOP playbooks, then SRE knowledge.
DEFAULT_COLLECTIONS: tuple[str, ...] = (
    COLLECTION_ACTION_EXPERIENCE,
    COLLECTION_SOP,
    COLLECTION_SRE_KNOWLEDGE,
)


@dataclass
class BrainHit:
    score: float
    point_id: str
    collection: str
    summary: str


@dataclass
class BrainTurn:
    turn: int
    query: str
    top_score: float
    hits: list[BrainHit] = field(default_factory=list)


@dataclass
class BrainResult:
    trace: str
    turns: list[BrainTurn]
    accumulated_context: str
    top_score: float
    confident: bool
    answer: str = ""           # best recalled advisory text when confident
    answer_point_id: str = ""

    @property
    def turn_count(self) -> int:
        return len(self.turns)


def _payload_summary(payload: dict[str, Any]) -> str:
    """Reduce a recalled payload to a one-line advisory summary (arg KEYS only)."""
    if not isinstance(payload, dict):
        return str(payload)[:300]
    for key in ("advisory", "summary", "lesson", "root_cause", "text", "content", "answer"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:400]
    # Fall back to a compact join of scalar fields (no secret values — keys only for dicts).
    parts = [f"{k}={v}" for k, v in payload.items() if isinstance(v, (str, int, float))][:6]
    return " ".join(parts)[:400] or json.dumps(payload, default=str)[:300]


def _refine_query(initial_query: str, snippets: list[str]) -> str:
    """Build the next-turn query from accumulated knowledge.

    Carries the alert symptom forward (so the session stays anchored to ONE alert)
    and appends the most recent learned snippet so the next search explores the
    adjacent/causal neighbourhood instead of repeating the same lookup.
    """
    tail = snippets[-1] if snippets else ""
    base = initial_query.strip()[:1500]
    if not tail:
        return base
    return f"{base}\nKNOWN SO FAR: {tail[:600]}"


async def _search_collection(
    vs: Any, *, query: str, collection: str, llm: Any, embed_model: str
) -> list[BrainHit]:
    try:
        resp = await vs.similarity_search(
            query, collection, llm=llm, embed_model=embed_model,
            limit=PER_TURN_LIMIT, score_threshold=MIN_HIT_SCORE,
        )
    except Exception as exc:  # noqa: BLE001 — brain is best-effort, never fails ingest
        logger.debug("event=redis_brain_search_failed collection=%s err=%r", collection, exc)
        return []
    hits: list[BrainHit] = []
    for p in getattr(resp, "points", None) or []:
        hits.append(BrainHit(
            score=round(float(p.score or 0), 3),
            point_id=str(p.id or ""),
            collection=collection,
            summary=_payload_summary(p.payload or {}),
        ))
    return hits


async def run_redis_brain(
    ctx: Any,
    *,
    trace: str,
    initial_query: str,
    collections: tuple[str, ...] = DEFAULT_COLLECTIONS,
    max_turns: int = DEFAULT_MAX_TURNS,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> BrainResult:
    """Run the multi-turn Redis RAG loop for one alert and persist the session.

    Best-effort: any failure returns a low-confidence empty result so the caller
    can fall back to the LLM. Never raises.
    """
    vs = getattr(ctx, "vector_store", None)
    llm = getattr(ctx, "llm", None)
    ws = getattr(ctx, "settings", None)
    redis = getattr(ctx, "redis", None)
    result = BrainResult(trace=trace, turns=[], accumulated_context="", top_score=0.0, confident=False)
    if vs is None or llm is None:
        return result

    embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")
    seen_ids: set[str] = set()
    snippets: list[str] = []
    best_score = 0.0
    best_answer = ""
    best_point_id = ""
    query = initial_query

    for turn_no in range(1, max_turns + 1):
        turn_hits: list[BrainHit] = []
        for coll in collections:
            for h in await _search_collection(vs, query=query, collection=coll, llm=llm, embed_model=embed_model):
                if h.point_id and h.point_id in seen_ids:
                    continue  # carry-forward de-dup: never re-count knowledge across turns
                if h.point_id:
                    seen_ids.add(h.point_id)
                turn_hits.append(h)

        turn_hits.sort(key=lambda x: x.score, reverse=True)
        turn_top = turn_hits[0].score if turn_hits else 0.0
        for h in turn_hits:
            if len(snippets) >= MAX_CONTEXT_SNIPPETS:
                break
            snippets.append(f"[{h.collection} score={h.score}] {h.summary}")
        if turn_hits and turn_hits[0].score > best_score:
            best_score = turn_hits[0].score
            best_answer = turn_hits[0].summary
            best_point_id = turn_hits[0].point_id

        result.turns.append(BrainTurn(turn=turn_no, query=query[:600], top_score=turn_top, hits=turn_hits))

        # Stop conditions: confident enough, or this turn learned nothing new.
        if best_score >= confidence_threshold or not turn_hits:
            break
        query = _refine_query(initial_query, snippets)

    result.accumulated_context = "\n".join(snippets)
    result.top_score = round(best_score, 3)
    result.confident = best_score >= confidence_threshold
    result.answer = best_answer
    result.answer_point_id = best_point_id

    await _persist_session(redis, result)
    logger.info(
        "event=redis_brain_done trace=%s turns=%d top_score=%.3f confident=%s snippets=%d",
        trace, result.turn_count, result.top_score, result.confident, len(snippets),
    )
    return result


async def _persist_session(redis: Any, result: BrainResult) -> None:
    if redis is None or not result.trace:
        return
    doc = {
        "trace_id": result.trace,
        "top_score": result.top_score,
        "confident": result.confident,
        "turn_count": result.turn_count,
        "answer": result.answer,
        "answer_point_id": result.answer_point_id,
        "updated_at": time.time(),
        "turns": [
            {
                "turn": t.turn,
                "query": t.query,
                "top_score": t.top_score,
                "hits": [
                    {"score": h.score, "point_id": h.point_id, "collection": h.collection, "summary": h.summary}
                    for h in t.hits
                ],
            }
            for t in result.turns
        ],
    }
    try:
        await redis.setex(
            SESSION_KEY.format(trace=result.trace),
            SESSION_TTL_SEC,
            json.dumps(doc, ensure_ascii=False, default=str),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("event=redis_brain_persist_failed trace=%s err=%r", result.trace, exc)
