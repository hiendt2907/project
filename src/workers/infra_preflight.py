"""Preflight: gợi ý namespace/pod từ **labels/alert text** + semantic search (pgvector) — không Redis map/topology."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from rag.pgvector_store import COLLECTION_INFRA_TOPOLOGY, COLLECTION_K8S_EXPERT
from workers.clarification import is_scope_ambiguous_cpu_ram

logger = logging.getLogger(__name__)


def _embedding_from_ollama(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


@dataclass
class LearnedContext:
    namespace: str | None = None
    pod_name: str | None = None
    service_name: str | None = None
    matched_token: str | None = None
    infra_blocks: list[str] = field(default_factory=list)
    had_vector_search: bool = False
    clarification_bypass: bool = False
    embed_vector: list[float] | None = None


def _apply_bypass_heuristic(text: str, learned: LearnedContext) -> None:
    t = (text or "").lower()
    if learned.namespace and learned.namespace.lower() in t.replace("_", "-"):
        learned.clarification_bypass = True
    if learned.matched_token and learned.matched_token.lower() in t:
        learned.clarification_bypass = True
    if learned.pod_name and learned.pod_name.lower() in t:
        learned.clarification_bypass = True
    if learned.service_name and learned.service_name.lower() in t:
        learned.clarification_bypass = True


def _merge_hints(learned: LearnedContext, hints: dict[str, str] | None) -> None:
    if not hints:
        return
    ns = (hints.get("namespace") or "").strip()
    pod = (hints.get("pod_name") or hints.get("pod") or "").strip()
    svc = (hints.get("service_name") or "").strip()
    if ns and not learned.namespace:
        learned.namespace = ns
        learned.matched_token = learned.matched_token or ns
    if pod and not learned.pod_name:
        learned.pod_name = pod
    if svc and not learned.service_name:
        learned.service_name = svc


async def preflight_infra_kb(
    ctx: Any,
    user_text: str,
    *,
    hints: dict[str, str] | None = None,
) -> LearnedContext:
    """
    State machine / alert labels (``hints``) trước; sau đó semantic search (expert collection + ``infra_topology``).
    Không còn ``infra:learned:byname`` hay ``state:current_topology`` trên Redis.
    """
    learned = LearnedContext()
    raw = (user_text or "").strip()
    if not raw:
        return learned

    _merge_hints(learned, hints)

    if learned.namespace:
        learned.infra_blocks.append(
            f"[CONTEXT: alert_or_hints]\nnamespace={learned.namespace} pod={learned.pod_name or '-'} service={learned.service_name or '-'}"
        )

    _raw_ex = getattr(getattr(ctx, "settings", None), "pgvector_collection_k8s_expert", None)
    expert_coll = (
        _raw_ex.strip()
        if isinstance(_raw_ex, str) and _raw_ex.strip()
        else COLLECTION_K8S_EXPERT
    )

    skip_vector_early = is_scope_ambiguous_cpu_ram(raw) and not learned.namespace
    if skip_vector_early:
        _apply_bypass_heuristic(raw, learned)
        return learned

    if learned.namespace:
        _apply_bypass_heuristic(raw, learned)
        return learned

    if len(raw) < 8:
        _apply_bypass_heuristic(raw, learned)
        return learned

    try:
        emb_resp = await ctx.ollama.embed(
            model=ctx.settings.embed_model,
            input=raw[:2000],
            keep_alive=ctx.settings.ollama_keep_alive,
        )
        vector = _embedding_from_ollama(emb_resp)
        learned.embed_vector = vector
        learned.had_vector_search = True

        for coll, label, lim, thresh in (
            (expert_coll, "k8s_expert", 3, 0.48),
            (COLLECTION_INFRA_TOPOLOGY, "infra_topology", 2, 0.52),
        ):
            try:
                resp = await ctx.vector_store.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=lim,
                    score_threshold=thresh,
                    with_payload=True,
                )
            except Exception as e:
                logger.debug("preflight vector %s: %s", coll, e)
                continue
            for i, pt in enumerate(resp.points or []):
                pl = dict(pt.payload or {})
                chunk = pl.get("text") or pl.get("summary") or ""
                if not chunk:
                    continue
                learned.infra_blocks.append(
                    f"[CONTEXT: {label} score={pt.score:.3f}]\n{chunk[:1800]}"
                )
                if coll == COLLECTION_INFRA_TOPOLOGY and i == 0 and pt.score >= 0.58:
                    if pl.get("namespace"):
                        learned.namespace = str(pl.get("namespace"))
                    if pl.get("pod_name"):
                        learned.pod_name = str(pl.get("pod_name"))
                    if pl.get("service_name"):
                        learned.service_name = str(pl.get("service_name"))
    except Exception as e:
        logger.debug("preflight embed/query: %s", e)

    _apply_bypass_heuristic(raw, learned)
    return learned
