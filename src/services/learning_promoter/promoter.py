"""Auto-Promote SOP Pipeline (S2.2).

After each VERIFIED_SUCCESS, increments a success counter for the pattern key.
When the counter crosses sop_promotion_min_success and FP rate is acceptable,
promotes the best action_experience record to SOP ledger.

Redis keys used (per pattern_key):
  omni:learn:promo:{pattern_key}  → HSET with keys: success, promoted_sop_id, promoted_at
  omni:matrix:auto:{pattern_key}  → STR, TTL=30d, value = sop_id (for incident matrix)

CRAT event: SOP_PROMOTED
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from services.audit_ledger.crat_event_types import CRAT_EVENT_SOP_PROMOTED

logger = logging.getLogger(__name__)

_PROMO_HASH_KEY = "omni:learn:promo:{pattern_key}"
_MATRIX_AUTO_KEY = "omni:matrix:auto:{pattern_key}"
_MATRIX_AUTO_TTL = 86400 * 30  # 30 days
_MIN_FP_SAMPLES = 10  # below this the FP rate is noise, treat as "unknown"


async def evaluate_for_promotion(
    ctx: Any,
    *,
    pattern_key: str,
    trace_id: str,
    tool_name: str,
    match_text: str,
    args_playbook: dict[str, Any],
) -> bool:
    """Call after each VERIFIED_SUCCESS.  Returns True if promotion happened."""
    if not pattern_key:
        return False

    ws = getattr(ctx, "settings", None)
    if not bool(getattr(ws, "omni_sop_auto_promote_enabled", True)):
        return False

    min_success = int(getattr(ws, "omni_sop_promotion_min_success", 3) or 3)
    max_fp_rate = float(getattr(ws, "omni_sop_promotion_max_fp_rate", 0.05) or 0.05)

    redis = getattr(ctx, "redis", None)
    if redis is None:
        return False

    promo_key = _PROMO_HASH_KEY.format(pattern_key=pattern_key)

    try:
        success_count = int(await redis.hincrby(promo_key, "success", 1))
        # Expire the hash 90 days after last update.
        await redis.expire(promo_key, 86400 * 90)
    except Exception as e:
        logger.warning("event=promo_counter_fail trace=%s err=%s", trace_id, e)
        return False

    # Check if already promoted — don't promote twice.
    try:
        already = await redis.hget(promo_key, "promoted_sop_id")
        if already:
            logger.debug("event=promo_already_promoted pattern=%s sop=%s", pattern_key, already)
            return False
    except Exception:
        pass

    if success_count < min_success:
        logger.debug(
            "event=promo_below_threshold pattern=%s success=%d min=%d",
            pattern_key, success_count, min_success,
        )
        return False

    # Quality gate. This promotion sets auto_execute=True, so a missing FP rate must
    # FAIL CLOSED: `_get_fp_rate` returns None when the KPI z-sets hold too few samples,
    # and those z-sets were empty in production (audit #9) — meaning the gate silently
    # passed every candidate through with zero quality evidence.
    fp_rate = await _get_fp_rate(redis, pattern_key)
    if fp_rate is None:
        if bool(getattr(ws, "omni_sop_promotion_require_fp_data", True)):
            logger.info(
                "event=promo_blocked_no_fp_data pattern=%s — refusing auto_execute "
                "promotion without quality evidence (FAIL_CLOSED)",
                pattern_key,
            )
            return False
    elif fp_rate > max_fp_rate:
        logger.info(
            "event=promo_fp_rate_too_high pattern=%s fp_rate=%.3f max=%.3f",
            pattern_key, fp_rate, max_fp_rate,
        )
        return False

    sop_id = await _upsert_sop_entry(
        ctx,
        pattern_key=pattern_key,
        tool_name=tool_name,
        match_text=match_text,
        args_playbook=args_playbook,
        success_count=success_count,
        trace_id=trace_id,
    )
    if sop_id is None:
        return False

    # Optional markdown export (disabled unless an export dir is configured).
    await _export_skill_markdown(
        export_dir=str(getattr(ws, "omni_sop_skill_export_dir", "") or ""),
        pattern_key=pattern_key,
        tool_name=tool_name,
        match_text=match_text,
        args_playbook=args_playbook,
        success_count=success_count,
        trace_id=trace_id,
    )

    # Mark as promoted.
    try:
        await redis.hset(promo_key, mapping={
            "promoted_sop_id": sop_id,
            "promoted_at": str(int(time.time())),
            "promoted_from_trace": trace_id,
        })
        # Flag for incident matrix auto-row pickup.
        await redis.setex(
            _MATRIX_AUTO_KEY.format(pattern_key=pattern_key),
            _MATRIX_AUTO_TTL,
            sop_id,
        )
    except Exception as e:
        logger.warning("event=promo_mark_fail trace=%s err=%s", trace_id, e)

    # Write CRAT block (best-effort — promotion is not fail-closed).
    await _write_promo_crat(ctx, pattern_key=pattern_key, sop_id=sop_id,
                            success_count=success_count, trace_id=trace_id)

    logger.info(
        "event=sop_promoted pattern=%s sop_id=%s success_count=%d trace=%s",
        pattern_key, sop_id, success_count, trace_id,
    )
    return True


async def _get_fp_rate(
    redis: Any, pattern_key: str, *, tenant_id: str = "default"
) -> float | None:
    """Approximate FP rate from tenant KPI counters (not per-pattern — best effort).

    Reads through ``read_outcome_rates`` so the key shape can never drift from what
    ``KPIStore`` writes; the previous hand-built ``omni:kpi:z:accepted`` keys did not
    match the writer's per-tenant keys and always returned 0 samples.
    """
    try:
        from workers.kpi_metrics import read_outcome_rates

        rates = await read_outcome_rates(redis, tenant_id=tenant_id)
        if int(rates.get("total") or 0) < _MIN_FP_SAMPLES:
            return None  # Not enough data yet.
        return rates.get("fp_rate")
    except Exception:
        return None


async def _upsert_sop_entry(
    ctx: Any,
    *,
    pattern_key: str,
    tool_name: str,
    match_text: str,
    args_playbook: dict[str, Any],
    success_count: int,
    trace_id: str,
) -> str | None:
    """Upsert a SOP entry into the Redis HNSW vector store."""
    try:
        from rag.pgvector_store import COLLECTION_SOP, EMBED_DIM, PointStruct
        from rag.sop_ledger import sop_payload_for_fast_path

        sop_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"auto-sop:{pattern_key}"))

        embed_input = f"{match_text} tool={tool_name}"[:4000]
        emb_resp = await ctx.llm.embed(
            model=ctx.settings.embed_model,
            input=embed_input,
        )
        vec = (emb_resp.get("embedding") or emb_resp.get("embeddings") or [[]])[0] \
            if "embeddings" in emb_resp else emb_resp.get("embedding", [])
        if not isinstance(vec, list):
            vec = list(vec)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]

        payload = sop_payload_for_fast_path(
            match_text=match_text[:8000],
            tool=tool_name,
            args=args_playbook,
            auto_execute=True,
            template_id=f"auto:{pattern_key[:32]}",
            variant_key=pattern_key,
        )
        payload["auto_promoted"] = True
        payload["auto_promoted_success_count"] = success_count
        payload["auto_promoted_from_trace"] = trace_id
        payload["auto_promoted_at"] = str(int(time.time()))

        await ctx.vector_store.upsert(
            collection_name=COLLECTION_SOP,
            points=[PointStruct(id=sop_id, vector=vec, payload=payload)],
        )
        return sop_id
    except Exception as e:
        logger.warning("event=promo_sop_upsert_fail trace=%s err=%s", trace_id, e)
        return None


async def _write_promo_crat(
    ctx: Any,
    *,
    pattern_key: str,
    sop_id: str,
    success_count: int,
    trace_id: str,
) -> None:
    """Write SOP_PROMOTED CRAT block — best-effort (not fail-closed)."""
    try:
        from services.audit_ledger.chain_writer import write_audit_block
        ws = getattr(ctx, "settings", None)
        kafka_topic = getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain")
        await write_audit_block(
            event_type=CRAT_EVENT_SOP_PROMOTED,
            trace_id=trace_id,
            payload={
                "pattern_key": pattern_key,
                "sop_id": sop_id,
                "success_count": success_count,
                "promoted_from_trace": trace_id,
                "auto_promoted": True,
            },
            redis=ctx.redis,
            kafka=ctx.kafka,
            kafka_topic=kafka_topic,
        )
    except Exception as e:
        logger.warning("event=promo_crat_write_fail trace=%s err=%s", trace_id, e)


async def _export_skill_markdown(
    *,
    export_dir: str,
    pattern_key: str,
    tool_name: str,
    match_text: str,
    args_playbook: dict[str, Any],
    success_count: int,
    trace_id: str,
) -> None:
    """Write the promoted SOP as markdown into *export_dir*; no-op when unset.

    Previously this hardcoded a developer's absolute workspace path
    (``/Users/<user>/project/.cursor/skills/learned``), which does not exist inside the
    worker pod and made a running service write into a source tree. The destination is
    now configuration, and the feature is off unless explicitly pointed somewhere.
    """
    if not export_dir.strip():
        return
    try:
        import os
        import re
        import asyncio

        # Sanitize pattern key to make a safe filename
        clean_key = re.sub(r'[^a-zA-Z0-9_\-]', '_', pattern_key)
        skills_dir = export_dir.strip()

        def write_sync():
            os.makedirs(skills_dir, exist_ok=True)
            filepath = os.path.join(skills_dir, f"auto-sop-{clean_key}.md")

            content = f"""---
name: auto-sop-{clean_key}
description: Automatically promoted SOP skill for {clean_key}. Diagnoses and remediates using tool {tool_name}.
origin: Omni Learning Promoter (Auto-Promoted)
---

# Auto-Promoted SOP: {clean_key}

## Context & Symptom Match
- **Symptom Pattern:** {match_text}
- **Auto-Execute:** True
- **Promoted From Trace:** {trace_id}
- **Success Count:** {success_count}

## Remediation Playbook
- **Tool:** {tool_name}
- **Arguments:** {args_playbook}

## Clinical SRE Verification Steps
1. Execute verification probes via `post_mutate_sdk_verify`.
2. Confirm workload rollout is healthy.
"""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            
        await asyncio.to_thread(write_sync)
        logger.info("event=sop_skill_exported pattern=%s path=.cursor/skills/learned/auto-sop-%s.md", pattern_key, clean_key)
    except Exception as e:
        logger.warning("event=sop_skill_export_fail pattern=%s err=%s", pattern_key, e)
