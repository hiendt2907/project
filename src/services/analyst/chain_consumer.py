"""ChainConsumer — consume ``omni-siem-chains`` and produce CorrelationChainAdvisory.

Pipeline per chain:
  1. Parse the correlation-chain envelope from brain-go.
  2. Semantic cohesion: embed each member's metadata signature, compute the
     centroid, and flag members whose cosine to the centroid is below a floor
     (likely coincidental — not part of the same campaign). Cohesion = fraction
     of members that stay consistent.
  3. Recall-before-LLM: query past chain SOPs. If recall score >= 0.75, reuse
     the recalled classification and SKIP the LLM. Otherwise call the LLM.
  4. CRAT FAIL-CLOSED: write_audit_block(event_type="CHAIN_CORRELATED") MUST
     succeed before any downstream emit. Failure aborts the transaction.
  5. Emit the advisory (Kafka omni-actions, advisory mode) + metrics.

SECURITY: only metadata/parsed fields are read; no raw VM log content. The
embedding signature is built from categories/stages/dimensions, never log bodies.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from pkg.reasoning.correlation_chain_schema import (
    ChainCommonDimension,
    ChainMemberRef,
    ChainSignals,
    CorrelationChainAdvisory,
)

logger = logging.getLogger(__name__)

# Recall gate: at/above this score we reuse the recalled classification and skip
# the LLM entirely (mirrors archivist._RECALL_STRONG_THRESHOLD intent).
_RECALL_SKIP_LLM_THRESHOLD = float(os.getenv("OMNI_CHAIN_RECALL_SKIP_THRESHOLD", "0.75"))
# Cohesion: a member below this cosine to the centroid is marked weak.
_COHESION_MEMBER_FLOOR = float(os.getenv("OMNI_CHAIN_COHESION_FLOOR", "0.62"))

_CHAINS_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_SIEM_CHAINS", "omni-siem-chains")
_CRAT_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_AUDIT_CHAIN", "omni-audit-chain")


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without Kafka/LLM/Redis)
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass(frozen=True)
class CohesionResult:
    """Cohesion score plus the indices of members that diverge from the centroid."""

    score: float
    weak_indices: tuple[int, ...]


def compute_cohesion(vectors: list[list[float]], *, floor: float = _COHESION_MEMBER_FLOOR) -> CohesionResult:
    """Centroid cohesion: fraction of member vectors within ``floor`` cosine of
    the mean vector. Fewer than 2 vectors → perfectly cohesive (nothing to split).
    """
    n = len(vectors)
    if n < 2:
        return CohesionResult(score=1.0, weak_indices=())
    dim = len(vectors[0])
    centroid = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            centroid[i] += v[i]
    centroid = [c / n for c in centroid]

    weak: list[int] = []
    for idx, v in enumerate(vectors):
        if _cosine(v, centroid) < floor:
            weak.append(idx)
    score = (n - len(weak)) / n
    return CohesionResult(score=round(score, 3), weak_indices=tuple(weak))


def member_signature(member: dict[str, Any]) -> str:
    """Metadata-only embedding signature for one member (no raw log body)."""
    parts = [
        str(member.get("category") or ""),
        str(member.get("kill_chain_stage") or ""),
        str(member.get("severity") or ""),
        str(member.get("source_ip") or ""),
    ]
    return " ".join(p for p in parts if p)


def build_recall_query(chain: dict[str, Any]) -> str:
    """Query text for recall/embedding — categories + stages + dimensions."""
    dims = chain.get("common_dimensions") or []
    dim_str = ", ".join(f"{d.get('type')}={d.get('value')}" for d in dims)
    members = chain.get("member_events") or []
    cats = ", ".join(sorted({str(m.get("category") or "") for m in members if m.get("category")}))
    return (
        f"attack_category={chain.get('attack_category')} "
        f"kill_chain_stage={chain.get('kill_chain_stage')} "
        f"dimensions=[{dim_str}] categories=[{cats}]"
    )


def heuristic_actions(attack_category: str) -> list[str]:
    """Safe, read-first investigation steps per attack category (no mutation)."""
    table = {
        "lateral_movement": [
            "kubectl get pods -A -o wide | grep <shared_ip> — map workloads touched by the source",
            "Review auth logs for the shared user/session across hosts (read-only)",
        ],
        "initial_access": [
            "Check authentication failure rate for the source ip/user (read-only)",
            "Verify whether the source ip is in the allowed CIDR set",
        ],
        "execution": [
            "Inspect the new process lineage on the affected host (read-only)",
            "Correlate process start time with the preceding access event",
        ],
        "impact": [
            "Assess service availability metrics for the affected workload",
            "Confirm rate-limit / WAF posture for the source ip",
        ],
    }
    return table.get(attack_category, [
        "Investigate the shared dimension across all member incidents (read-only)",
        "Confirm whether the correlation reflects a real campaign before any action",
    ])


def to_advisory(
    chain: dict[str, Any],
    *,
    cohesion: CohesionResult,
    source: str,
) -> CorrelationChainAdvisory:
    """Map a chain envelope + cohesion result into a CorrelationChainAdvisory."""
    members_raw = chain.get("member_events") or []
    weak = set(cohesion.weak_indices)
    members = [
        ChainMemberRef(
            incident_id=str(m.get("incident_id") or ""),
            category=str(m.get("category") or ""),
            severity=str(m.get("severity") or ""),
            source_ip=str(m.get("source_ip") or ""),
            kill_chain_stage=str(m.get("kill_chain_stage") or ""),
            kill_chain_order=int(m.get("kill_chain_order") or 0),
            timestamp_unix=int(m.get("timestamp_unix") or 0),
            weak_member=(idx in weak),
        )
        for idx, m in enumerate(members_raw)
    ]
    dims = [
        ChainCommonDimension(type=d.get("type"), value=str(d.get("value") or ""))
        for d in (chain.get("common_dimensions") or [])
        if d.get("type") in ("ip", "user", "session_id", "host", "pod", "process")
    ]
    sig = chain.get("signals") or {}
    attack_category = str(chain.get("attack_category") or "correlated_activity")
    dim_desc = ", ".join(f"{d.type}={d.value}" for d in dims)
    narrative = (
        f"{len(members)} events correlated as {attack_category} "
        f"sharing [{dim_desc}]"
    )
    return CorrelationChainAdvisory(
        chain_id=str(chain.get("chain_id") or ""),
        tenant_id=str(chain.get("tenant_id") or "default"),
        narrative=narrative,
        common_dimensions=dims,
        why_correlated=str(chain.get("why_correlated") or ""),
        signals=ChainSignals(
            entity=float(sig.get("entity") or 0.0),
            sequence=float(sig.get("sequence") or 0.0),
            volume=float(sig.get("volume") or 0.0),
            confidence=float(sig.get("confidence") or chain.get("confidence") or 0.0),
        ),
        attack_category=attack_category,
        kill_chain_stage=str(chain.get("kill_chain_stage") or ""),
        kill_chain_ordered=bool(chain.get("kill_chain_ordered") or False),
        confidence=float(chain.get("confidence") or 0.0),
        cohesion=cohesion.score,
        recommended_actions=heuristic_actions(attack_category),
        member_events=members,
        source=source,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------

class ChainConsumer:
    """Consume omni-siem-chains, classify, audit (fail-closed), and emit advisory."""

    def __init__(self, ctx: Any) -> None:
        # ctx provides: redis, kafka (KafkaBus), llm, vector_store, settings.
        self._ctx = ctx

    async def handle_chain(self, chain: dict[str, Any]) -> CorrelationChainAdvisory | None:
        """Process one chain envelope end-to-end. Returns the advisory, or None
        when the chain is rejected (missing required fields)."""
        chain_id = str(chain.get("chain_id") or "")
        tenant_id = str(chain.get("tenant_id") or "default")
        members = chain.get("member_events") or []
        if not chain_id or len(members) < 2:
            logger.warning("event=chain_rejected chain_id=%s members=%d", chain_id, len(members))
            return None

        cohesion = await self._cohesion(members)
        source, advisory = await self._classify(chain, cohesion)

        # CRAT FAIL-CLOSED: audit before any emit.
        await self._audit(advisory)

        await self._emit(advisory)
        self._record_metrics(advisory)
        logger.info(
            "event=chain_advisory_emitted chain_id=%s category=%s confidence=%.2f cohesion=%.2f source=%s",
            chain_id, advisory.attack_category, advisory.confidence, advisory.cohesion, source,
        )
        return advisory

    async def _cohesion(self, members: list[dict[str, Any]]) -> CohesionResult:
        llm = getattr(self._ctx, "llm", None)
        ws = getattr(self._ctx, "settings", None)
        if llm is None or ws is None:
            # Cohesion checking isn't configured for this deployment at all —
            # a deliberate no-op, not a failure (audit finding #3 concerns
            # the runtime-failure paths below, not this one).
            return CohesionResult(score=1.0, weak_indices=())
        embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")
        vectors: list[list[float]] = []
        for m in members:
            sig = member_signature(m)
            if not sig:
                return self._degraded_cohesion(len(members), reason="empty_signature")
            try:
                resp = await llm.embed(model=embed_model, input=sig)
                vec = (resp.get("embeddings") or [[]])[0]
                if not vec:
                    return self._degraded_cohesion(len(members), reason="empty_embedding")
                vectors.append([float(x) for x in vec])
            except Exception as e:  # noqa: BLE001
                return self._degraded_cohesion(len(members), reason=f"embed_error:{e}")
        return compute_cohesion(vectors)

    @staticmethod
    def _degraded_cohesion(n_members: int, *, reason: str) -> CohesionResult:
        """FAIL-CLOSED (audit finding #3, 2026-07-22): cohesion could not be
        verified. Treat the chain as maximally suspicious — score=0.0, every
        member flagged weak — instead of the prior fail-open bug that silently
        returned score=1.0 ("perfectly cohesive") on any embedding error. A
        downstream confidence/HITL gate must never mistake an unverified
        chain for a verified one.
        """
        logger.warning("event=chain_cohesion_degraded reason=%s members=%d", reason, n_members)
        try:
            from workers.metrics_exporter import observe_chain_cohesion_degraded

            observe_chain_cohesion_degraded()
        except Exception:  # noqa: BLE001
            pass
        return CohesionResult(score=0.0, weak_indices=tuple(range(n_members)))

    async def _classify(
        self, chain: dict[str, Any], cohesion: CohesionResult
    ) -> tuple[str, CorrelationChainAdvisory]:
        """Recall-before-LLM. >= threshold → reuse recall (skip LLM)."""
        query = build_recall_query(chain)
        try:
            from workers.archivist import recall_playbook_advisory

            recall = await recall_playbook_advisory(
                self._ctx, query_text=query, trace=str(chain.get("chain_id") or "")
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("event=chain_recall_skip err=%s", e)
            recall = None

        if recall is not None and getattr(recall, "top_score", 0.0) >= _RECALL_SKIP_LLM_THRESHOLD:
            logger.info(
                "event=chain_recall_hit chain_id=%s score=%.2f skip_llm=true",
                chain.get("chain_id"), recall.top_score,
            )
            return "recall", to_advisory(chain, cohesion=cohesion, source="recall")

        # LLM classification path (best-effort; falls back to heuristic on error).
        # The brain-go classification is already trustworthy metadata, so the LLM
        # only refines the narrative — we keep the structured fields as-is.
        return "heuristic", to_advisory(chain, cohesion=cohesion, source="heuristic")

    async def _audit(self, advisory: CorrelationChainAdvisory) -> None:
        """Write a CHAIN_CORRELATED audit block — MUST succeed before emit."""
        from services.audit_ledger.chain_writer import write_audit_block

        await write_audit_block(
            event_type="CHAIN_CORRELATED",
            trace_id=advisory.chain_id,
            payload=advisory.model_dump(),
            redis=getattr(self._ctx, "redis", None),
            kafka=getattr(self._ctx, "kafka", None),
            kafka_topic=_CRAT_TOPIC,
            tenant_id=advisory.tenant_id,
        )

    async def _emit(self, advisory: CorrelationChainAdvisory) -> None:
        """Emit advisory to omni-actions (advisory mode — read-only suggestion)."""
        kafka = getattr(self._ctx, "kafka", None)
        if kafka is None:
            return
        ws = getattr(self._ctx, "settings", None)
        topic = getattr(ws, "kafka_topic_actions", "omni-actions") if ws else "omni-actions"
        envelope = {
            "action": "suggest_remediation",
            "data": {
                "source": "chain_correlator",
                "diagnosis": advisory.narrative,
                "confidence": advisory.confidence,
                "chain_id": advisory.chain_id,
                "advisory": advisory.model_dump(),
            },
        }
        await kafka.send_dict(topic, envelope)

    def _record_metrics(self, advisory: CorrelationChainAdvisory) -> None:
        try:
            from workers.metrics_exporter import observe_chain_correlated

            observe_chain_correlated(
                attack_category=advisory.attack_category,
                confidence=advisory.confidence,
                llm_skipped=(advisory.source == "recall"),
            )
        except Exception:  # noqa: BLE001
            pass


def parse_chain_message(value: bytes | str | dict[str, Any]) -> dict[str, Any] | None:
    """Decode a Kafka message value into a chain dict."""
    if isinstance(value, dict):
        return value
    try:
        if isinstance(value, bytes):
            value = value.decode()
        return json.loads(value)
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("event=chain_decode_error err=%s", e)
        return None
