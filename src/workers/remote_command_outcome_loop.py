"""Close the remote-host recovery loop: durable command outcome -> pipeline trace.

Why this module exists
----------------------
The durable mutation channel (``gateway/routes/agent_runtime.py``) is a complete
delivery protocol — claim/fence/accept/progress/terminal, Redis record + Postgres
ledger — but it terminates *inside itself*. When a VM agent reports COMPLETED, the
gateway writes the record and stops. Nothing then:

  - marks the originating trace's ``EXECUTOR`` / ``FEEDBACK`` pipeline stages,
  - publishes the result onto ``omni-action-feedback`` (the canonical bus every
    other outcome in this system flows through),
  - writes a CRAT block proving the mutation actually happened on the host.

Measured 2026-08-02: 0 of 809 remote traces had ever reached the ``EXECUTOR``
stage. That was not only a gate problem — even a mutation that *did* execute (and
one now has, verified on cust-app) left the trace looking like it stopped at
DISPATCH. This loop is the missing hop.

Design constraints honoured
---------------------------
- **Bounded work-list, not a scan.** Reads the ``omni:autorecovery:pending`` ZSET
  that ``auto_recovery_bridge`` populates at dispatch time. Never ``KEYS``/``SCAN``
  over ``omni:cmd:rec:*``, which would walk every tenant's command space.
- **CRAT is not optional.** A host mutation that completed with no audit block is
  a compliance hole, so a ledger failure keeps the entry pending and retries
  rather than dropping it.
- **Terminal, not re-planned.** Remote recovery outcomes are marked terminal
  before publishing so the K8s-oriented autonomous re-planner in
  ``autonomous_feedback_loop`` ingests them for stage-marking and stops, instead
  of trying to plan a follow-up K8s mutation for a systemd unit on a VM.
- The gateway owns delivery state; this loop only *reads* command records. It
  never mutates a command's state machine.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any

from pkg.observability.pipeline_stages import mark_stage
from workers.auto_recovery_bridge import PENDING_KEY

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
# Entries older than this are abandoned: the gateway's own TTL on an active
# command record is 24h, so a pending entry that has outlived that has no record
# left to reconcile against and would otherwise be retried forever.
_ABANDON_AFTER_S = 86_400
_BATCH = 50

_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "ESCALATED", "EXPIRED"})
# Only COMPLETED means the host actually reached the intended state. FAILED and
# ESCALATED are real outcomes too and must be recorded — but as stage failures.
_SUCCESS_STATES = frozenset({"COMPLETED"})


def _meta_key(tenant_id: str, command_id: str) -> str:
    return f"omni:autorecovery:meta:{tenant_id}:{command_id}"


def _rec_key(tenant_id: str, command_id: str) -> str:
    # Mirrors gateway.routes.agent_runtime._rec_key. Duplicated rather than
    # imported because src/workers must not import src/gateway (and vice versa);
    # the Redis layout is the documented contract between them.
    return f"omni:cmd:rec:{tenant_id}:{command_id}"


def _decode(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def split_member(member: Any) -> tuple[str, str] | None:
    """``"{tenant}|{command_id}"`` -> parts. None for anything malformed."""
    if isinstance(member, bytes):
        member = member.decode()
    text = str(member)
    tenant, sep, command_id = text.partition("|")
    if not sep or not tenant or not command_id:
        return None
    return tenant, command_id


def outcome_exit_code(record: dict) -> int:
    """Prefer the agent's own reported rc; fall back to the delivery state.

    The agent reports ``outcome.rc`` (0 on a verified recovery). A record that
    reached a terminal state without an rc — EXPIRED, for instance, which the
    gateway writes itself when no agent ever claimed the command — still has to
    produce a non-zero code so the trace shows a failure rather than a silent
    success.
    """
    outcome = record.get("outcome")
    if isinstance(outcome, dict) and outcome.get("rc") is not None:
        try:
            return int(outcome["rc"])
        except (TypeError, ValueError):
            return 1
    return 0 if record.get("state") in _SUCCESS_STATES else 1


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    return []


def _args_hash(args: dict[str, Any]) -> str:
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


async def _upsert_action_experience_on_success(
    ctx: Any, *, trace_id: str, command_id: str, capability: str, unit: str,
    outcome: dict[str, Any],
) -> None:
    """Teach the reflex (``remote_known_fix.try_remote_known_fix`` /
    ``known_fix_resolver.find_known_fix_candidate``) from a REAL, CRAT-audited
    remote recovery. Without this, ``_try_known_fix_reflex`` in
    ``knowledge_pipeline.py`` has no data for capability class ``systemd.*`` —
    it can only ever fire once at least one success has been recorded here.

    Best-effort by design: a learning-write failure must not turn an already
    CRAT-audited, already-published success into a retry."""
    try:
        from execution.memory_normalize import canonical_symptom_text
        from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE, EMBED_DIM, PointStruct

        reason = str(outcome.get("reason") or "")[:500]
        evidence = "; ".join(str(e) for e in (outcome.get("evidence") or []))[:800]
        lesson = f"[remote recovery success] capability={capability} unit={unit} {reason}".strip()
        symptom_raw = f"{capability} on unit {unit}: {reason}\n{evidence}".strip()
        strip_pods = bool(getattr(ctx.settings, "memory_canonical_strip_pods", True))
        symptom_text = canonical_symptom_text(symptom_raw[:4000], strip_pods=strip_pods)

        emb = await ctx.llm.embed(model=ctx.settings.embed_model, input=symptom_text[:4000])
        vec = _embedding_from_response(emb)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]

        args = {"unit": unit}
        args_hash = _args_hash(args)
        point_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"remote-outcome:{trace_id}:{capability}:{args_hash}",
        ))
        payload = {
            "memory_kind": "playbook",
            "symptom_text": symptom_text[:2000],
            "lesson": lesson[:1200],
            "routing_source": "remote_command_outcome",
            "tool": capability,
            "args": args,
            "args_hash": args_hash,
            "auto_execute": True,
            "match_text": symptom_text[:2000],
            "trace_id": trace_id,
            "command_id": command_id,
            "exec_outcome": "success",
            "verification_result": "pass" if outcome.get("verified") else "unverified",
            "safety_flag": "normal",
            "ts": str(int(time.time())),
        }
        await ctx.vector_store.upsert(
            collection_name=COLLECTION_ACTION_EXPERIENCE,
            points=[PointStruct(id=point_id, vector=vec, payload=payload)],
        )
    except Exception as exc:  # noqa: BLE001 — learning write is best-effort
        logger.debug(
            "event=remote_outcome_learning_upsert_skip trace=%s command_id=%s err=%s",
            trace_id, command_id, exc,
        )


async def _write_outcome_audit(ctx: Any, *, record: dict, meta: dict, trace_id: str,
                               tenant_id: str) -> None:
    """CRAT block for the executed mutation. Raises on failure — caller retries."""
    from services.audit_ledger.chain_writer import write_audit_block
    from services.audit_ledger.crat_event_types import CRAT_EVENT_ADVISORY_DISPATCHED

    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    await write_audit_block(
        event_type=CRAT_EVENT_ADVISORY_DISPATCHED,
        trace_id=trace_id,
        payload={
            "source": "remote_command_outcome_loop",
            "phase": "terminal",
            "command_id": record.get("command_id", ""),
            "agent_id": record.get("agent_id", ""),
            "tenant_id": tenant_id,
            "unit": meta.get("unit", ""),
            "capability": meta.get("capability", ""),
            "state": record.get("state", ""),
            "rc": outcome_exit_code(record),
            "status": str(outcome.get("status") or ""),
            "reason": str(outcome.get("reason") or "")[:500],
            "evidence": [str(e) for e in (outcome.get("evidence") or [])][:10],
            "verified": bool(outcome.get("verified", False)),
            "delivery_attempt": record.get("delivery_attempt", 0),
            "action_id": record.get("action_id", ""),
            "canonical_scope": record.get("canonical_scope", ""),
        },
        redis=ctx.redis,
        kafka=ctx.kafka,
        kafka_topic=getattr(ctx.settings, "kafka_topic_audit_chain", "omni-audit-chain"),
        tenant_id=tenant_id,
    )


async def reconcile_one(ctx: Any, tenant_id: str, command_id: str) -> str:
    """Reconcile a single dispatched command. Returns a disposition string:

    ``"pending"``   still in flight, leave on the work-list
    ``"done"``      terminal, fully reconciled, remove from the work-list
    ``"retry"``     terminal but reconciliation failed (e.g. CRAT down), keep it
    ``"abandoned"`` no record left to reconcile against
    """
    record = _decode(await ctx.redis.get(_rec_key(tenant_id, command_id)))
    if record is None:
        logger.warning("event=remote_outcome_record_missing tenant=%s command_id=%s",
                       tenant_id, command_id)
        return "abandoned"
    state = str(record.get("state") or "")
    if state not in _TERMINAL_STATES:
        return "pending"

    meta = _decode(await ctx.redis.get(_meta_key(tenant_id, command_id))) or {}
    trace_id = str(meta.get("trace_id") or record.get("incident_id") or "")
    if not trace_id:
        logger.warning("event=remote_outcome_no_trace command_id=%s state=%s", command_id, state)
        return "abandoned"

    rc = outcome_exit_code(record)
    ok = state in _SUCCESS_STATES and rc == 0
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    unit = meta.get("unit") or record.get("canonical_scope", "")

    # CRAT first: the audit block must exist before the outcome is broadcast, the
    # same ordering the dispatch side uses. A ledger failure means retry, never
    # "publish anyway".
    try:
        await _write_outcome_audit(ctx, record=record, meta=meta, trace_id=trace_id,
                                   tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 — includes AuditLedgerError; retry next tick
        logger.critical(
            "event=audit_chain_write_failed phase=remote_command_outcome trace=%s "
            "command_id=%s err=%s FAIL_CLOSED — outcome not published, will retry",
            trace_id, command_id, exc,
        )
        return "retry"

    await mark_stage(
        ctx.redis, trace_id, "EXECUTOR", "ok" if ok else "fail",
        detail=f"state={state} unit={unit} rc={rc} "
               f"reason={str(outcome.get('reason') or '')[:120]}",
    )

    if ok:
        await _upsert_action_experience_on_success(
            ctx, trace_id=trace_id, command_id=command_id,
            capability=str(meta.get("capability") or "systemd.restart_unit"),
            unit=str(unit), outcome=outcome,
        )

    # Mark the trace terminal BEFORE publishing. handle_action_feedback_envelope
    # marks the FEEDBACK stage and then short-circuits on this key, so the outcome
    # lands on the canonical bus and gets its stage without waking the K8s
    # re-planner — which has no meaningful next move for a systemd unit on a VM
    # and would otherwise burn attempts trying to find one.
    try:
        await ctx.redis.set(f"omni:autonomous:terminal:{trace_id}",
                            "remote_recovery_terminal", ex=86_400)
    except Exception as exc:  # noqa: BLE001 — best effort; stage marking still happens
        logger.warning("event=remote_outcome_terminal_flag_failed trace=%s err=%s",
                       trace_id, exc)

    from workers.autonomous_execute import publish_action_feedback

    await publish_action_feedback(
        ctx,
        trace_id=trace_id,
        tool_name=str(meta.get("capability") or "systemd.restart_unit"),
        correlation_id=command_id,
        stdout="; ".join(str(e) for e in (outcome.get("evidence") or []))[:2000],
        stderr="" if ok else str(outcome.get("reason") or "")[:2000],
        exit_code=rc,
        status="ok" if ok else "failed",
        mutate_args={"unit": str(unit), "agent_id": str(record.get("agent_id") or ""),
                     "command_id": command_id, "host_identity": str(meta.get("agent_id") or "")},
    )
    await mark_stage(
        ctx.redis, trace_id, "FEEDBACK", "ok" if ok else "fail",
        detail=f"remote agent outcome state={state} rc={rc}",
    )
    logger.info(
        "event=remote_command_outcome_reconciled trace=%s command_id=%s state=%s rc=%s unit=%s",
        trace_id, command_id, state, rc, unit,
    )
    return "done"


async def drain_once(ctx: Any) -> dict[str, int]:
    """One reconciliation pass over the pending work-list. Returns a tally."""
    tally = {"pending": 0, "done": 0, "retry": 0, "abandoned": 0}
    try:
        members = await ctx.redis.zrange(PENDING_KEY, 0, _BATCH - 1)
    except Exception as exc:  # noqa: BLE001 — Redis blip; next tick retries
        logger.warning("event=remote_outcome_zrange_failed err=%s", exc)
        return tally

    now = time.time()
    for member in members or []:
        parts = split_member(member)
        if parts is None:
            await ctx.redis.zrem(PENDING_KEY, member)
            continue
        tenant_id, command_id = parts
        try:
            disposition = await reconcile_one(ctx, tenant_id, command_id)
        except Exception as exc:  # noqa: BLE001 — one bad entry must not stall the loop
            logger.exception("event=remote_outcome_reconcile_error command_id=%s err=%s",
                             command_id, exc)
            disposition = "retry"

        if disposition == "pending":
            score = await ctx.redis.zscore(PENDING_KEY, member)
            if score is not None and now - float(score) > _ABANDON_AFTER_S:
                logger.warning("event=remote_outcome_abandoned_stale command_id=%s", command_id)
                await ctx.redis.zrem(PENDING_KEY, member)
                tally["abandoned"] += 1
                continue
        if disposition in ("done", "abandoned"):
            await ctx.redis.zrem(PENDING_KEY, member)
        tally[disposition] += 1
    return tally


async def remote_command_outcome_loop(ctx: Any, stop: Any) -> None:
    """Periodic reconciliation of dispatched remote recovery commands."""
    logger.info("remote_command_outcome_loop started interval=%.1fs", POLL_INTERVAL_S)
    while not getattr(stop, "is_set", lambda: False)():
        try:
            tally = await drain_once(ctx)
            if tally["done"] or tally["retry"]:
                logger.info("event=remote_outcome_drain %s", tally)
        except Exception as exc:  # noqa: BLE001 — loop must survive any single failure
            logger.exception("remote_command_outcome_loop: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
        except (asyncio.TimeoutError, AttributeError):
            pass
