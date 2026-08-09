"""Onboarding pipeline — step-3 của agent/plans/PLAN_onboarding_ops_agent.md.

Worker-side orchestration (Kafka/ctx/Telegram/admin_repo-aware) on top of the
dependency-light accumulation+Mermaid logic in pkg.onboarding.discovery_doc
(shared with the gateway's read-only diagram endpoint + handover-doc upload).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from pkg.onboarding import discovery_doc as dd

from workers.handler_context import WorkerHandlerContext
from workers.pipeline_stages import mark_stage

logger = logging.getLogger(__name__)


async def accumulate_discovery_evidence(ctx: WorkerHandlerContext, ev_doc: dict[str, Any]) -> None:
    """A2/A3: fold one DiscoveryEvidence probe result into the per-tenant doc hash."""
    tenant_id = str(ev_doc.get("tenant_id") or "default")
    probe = str(ev_doc.get("probe") or "unknown")
    trace = str(ev_doc.get("trace_id") or "")
    fact = ev_doc.get("extracted_fact") or {}
    if isinstance(fact, str):
        try:
            fact = json.loads(fact)
        except Exception:
            fact = {}
    discovery_data = fact.get("discovery_data") if isinstance(fact, dict) else None
    if not isinstance(discovery_data, dict):
        logger.warning("onboarding_pipeline: empty discovery_data probe=%s tenant=%s", probe, tenant_id)
        return

    # IT-7: per-host slot — multi-host tenant không còn ghi đè fact của nhau
    hostname = str(
        ev_doc.get("namespace") or ev_doc.get("hostname")
        or (fact.get("hostname") if isinstance(fact, dict) else "") or ""
    )
    try:
        await dd.accumulate_probe_fact(ctx.redis, tenant_id, probe, discovery_data, hostname=hostname)
    except Exception as exc:  # noqa: BLE001 — accumulation is best-effort, never blocks consumer
        logger.warning("onboarding_pipeline: accumulate failed tenant=%s probe=%s err=%s", tenant_id, probe, exc)
        return

    if trace:
        await mark_stage(ctx.redis, trace, "EVIDENCE", "ok", detail=f"onboarding_accumulated probe={probe}", signal_kind="learning")

    await _detect_gaps_and_ask(ctx, tenant_id, probe, discovery_data)
    try:
        await dd.regenerate_diagrams(ctx.redis, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("onboarding_pipeline: diagram regen failed tenant=%s err=%s", tenant_id, exc)
    readiness = await recompute_readiness(ctx, tenant_id)
    await _persist_onboarding_mission(ctx, tenant_id, readiness=readiness, probe=probe)

    # Slice O1 (additive, dual-write): project the same evidence into the canonical
    # AOIP Observation/Fact/SystemModel twin. Isolated on purpose — a failure here
    # must never lose the legacy discovery_doc write above (already committed) and
    # must never be reported as a successful twin update.
    await _project_into_system_model(ctx, ev_doc, tenant_id=tenant_id, trace=trace)


async def _project_into_system_model(
    ctx: WorkerHandlerContext, ev_doc: dict[str, Any], *, tenant_id: str, trace: str,
) -> None:
    from aoip.onboarding_projection import project_facts, resolve_ip_to_host_map, to_observation
    from aoip.system_model_store import fold_and_persist

    # coerce_evidence_dict() (pkg/reasoning/schema.py) promotes agent_id/hostname
    # to top-level fields precisely because the gateway nests them inside
    # extracted_fact, which gets truncated to 2000 chars — a large discovery_data
    # payload (e.g. long process_list) can silently cut them off. Prefer the
    # promoted top-level fields; fall back to the (possibly-truncated) nested
    # copy only for callers/tests that bypass coerce_evidence_dict.
    fact = ev_doc.get("extracted_fact") or {}
    if isinstance(fact, str):
        try:
            fact = json.loads(fact)
        except Exception:
            fact = {}
    agent_id = str(ev_doc.get("agent_id") or (fact.get("agent_id") if isinstance(fact, dict) else None) or "unknown")
    hostname = ev_doc.get("hostname") or (fact.get("hostname") if isinstance(fact, dict) else None)
    host = str(ev_doc.get("namespace") or hostname or agent_id or "unknown-host")
    try:
        observation = to_observation(ev_doc, tenant_id=tenant_id, agent_id=agent_id, host=host)
        if observation is None:
            return  # unsupported probe / malformed evidence — not an error
        ip_to_host = None
        if observation.data.get("probe") == "connection_scan":
            ip_to_host = await resolve_ip_to_host_map(ctx.redis, tenant_id)
        new_facts = project_facts(observation, ip_to_host=ip_to_host)
        if not new_facts:
            return
        _model, _revision, contradictions = await fold_and_persist(
            ctx.redis, tenant_id, new_facts, source=observation.source,
        )
    except Exception as exc:  # noqa: BLE001 — additive path, never blocks/breaks the legacy write
        logger.error(
            "onboarding_pipeline: system_model projection failed tenant=%s agent=%s host=%s err=%s",
            tenant_id, agent_id, host, exc,
        )
        return

    if trace:
        await mark_stage(
            ctx.redis, trace, "SYSTEM_MODEL", "ok",
            detail=f"aoip_fact_projected probe={ev_doc.get('probe')} contradictions={len(contradictions)}",
            signal_kind="learning",
        )
    if contradictions:
        logger.warning(
            "onboarding_pipeline: system_model contradiction tenant=%s count=%d",
            tenant_id, len(contradictions),
        )

    # Slice O2B (additive): re-derive the Competency Matrix for the entities this
    # evidence touched and open/refresh structured Unknowns — isolated the same way
    # as the O1 projection above, never allowed to affect the legacy write.
    await _sync_understanding_gaps(ctx, ev_doc, tenant_id=tenant_id, host=host, new_facts=new_facts)


async def _sync_understanding_gaps(
    ctx: WorkerHandlerContext, ev_doc: dict[str, Any], *, tenant_id: str, host: str, new_facts: Any,
) -> None:
    """Bookkeeping only: open/refresh structured Unknown records for whatever
    the Competency Matrix currently reports as UNKNOWN/CONTRADICTED.

    Deliberately does NOT create Questions or send Telegram here — a single
    evidence event can touch a dozen still-empty facets (owner, sla, runbook,
    ...) and firing one question per facet per event would flood the tenant.
    Turning an open Unknown into an actual Question is a separate, deliberate
    step (``question_lifecycle.ensure_question_for_unknown``) meant to be
    invoked by a batched/paced caller, not inline on every probe message.
    """
    from aoip.competency_matrix import build_entity_competency_from_store
    from aoip.question_lifecycle import sync_unknowns_from_competency
    from aoip.system_graph import make_node

    host_node = make_node("host", host)
    entities: list[tuple[str, str]] = [("host", host_node)]
    for f in new_facts:
        if f.predicate == "runs_service":
            entities.append(("service", make_node("service", f.obj)))

    for entity_type, entity_id in entities:
        try:
            comp = await build_entity_competency_from_store(
                ctx.redis, tenant_id, entity_type=entity_type, entity_id=entity_id,
            )
            await sync_unknowns_from_competency(ctx.redis, tenant_id, comp)
        except Exception as exc:  # noqa: BLE001 — additive path, never blocks the legacy write
            logger.warning(
                "onboarding_pipeline: understanding-gap sync failed tenant=%s entity=%s err=%s",
                tenant_id, entity_id, exc,
            )


async def accumulate_handover_document(
    ctx: WorkerHandlerContext, tenant_id: str, *, filename: str, content: str,
) -> None:
    """A8: manually-uploaded handover doc feeds the same accumulation pipeline as
    doc_snapshot probe evidence — does not need the remote agent/Kafka.

    Data residency: content is only hashed here, never persisted — see
    dd.accumulate_probe_fact's residency sanitization."""
    discovery_data = {"documents": [{"path": filename, "content": content}]}
    await dd.accumulate_probe_fact(ctx.redis, tenant_id, "doc_snapshot", discovery_data)
    await dd.regenerate_diagrams(ctx.redis, tenant_id)
    await recompute_readiness(ctx, tenant_id)


async def _detect_gaps_and_ask(
    ctx: WorkerHandlerContext, tenant_id: str, probe: str, discovery_data: dict[str, Any],
) -> None:
    """A5: detect an obvious gap in this probe's data, ask the tenant via Telegram.

    Bước 7 compatibility — deliberately kept running unchanged alongside the
    newer entity/facet-aware ``aoip.question_lifecycle`` (Unknown -> Question ->
    Answer -> Claim). This path is per-probe, free-text, dedups only crudely
    (``api_access`` case scans existing question text), and never turns an
    answer into a verifiable Claim — it just records that a question was asked
    (``resolve_question`` below). It writes to
    ``pkg.onboarding.discovery_doc.QUESTIONS_KEY``
    (``omni:onboarding:questions:{tenant_id}``), a DIFFERENT Redis namespace
    from ``question_lifecycle.QUESTIONS_KEY``
    (``omni:aoip:questions:{tenant_id}``) — the two paths never read or write
    each other's records. Full boundary/rationale (field-by-field, why this is
    not an ``INV_SINGLE_SOURCE_OF_TRUTH`` violation, decision guide for future
    code):`docs/architecture/QUESTION_PATH_BOUNDARY.md`.
    """
    question: str | None = None
    if probe == "service_topology":
        services = discovery_data.get("services") or []
        unnamed = [s for s in services if not str(s.get("description") or "").strip()]
        if services and len(unnamed) == len(services):
            question = (
                f"Onboarding ({tenant_id}): phát hiện {len(services)} service đang chạy nhưng "
                "chưa rõ mục đích nghiệp vụ — bạn mô tả ngắn gọn từng service được không?"
            )
    elif probe == "port_scan":
        ports = discovery_data.get("listening_ports") or []
        unknown = [p for p in ports if not str(p.get("service") or "").strip()]
        if unknown:
            port_list = ", ".join(str(p.get("port")) for p in unknown[:10])
            question = (
                f"Onboarding ({tenant_id}): các cổng {port_list} đang mở nhưng chưa rõ dịch vụ nào "
                "sử dụng — bạn xác nhận giúp không?"
            )
    elif probe == "api_access":
        # Runtime access metadata is useful for correlation, but it is not an
        # API contract. Ask once when routes are seen without a discovered or
        # uploaded OpenAPI/Swagger document; the UI can also resolve this by
        # uploading the contract through /onboarding/handover-doc.
        try:
            raw_doc = await ctx.redis.hgetall(dd.DOC_KEY.format(tenant_id=tenant_id))
            has_contract = any(str(key).startswith("api_contract") for key in raw_doc)
            if not has_contract:
                existing = await ctx.redis.hgetall(dd.QUESTIONS_KEY.format(tenant_id=tenant_id))
                already_asked = any("OpenAPI/Swagger" in str(value) for value in existing.values())
                if not already_asked:
                    question = (
                        f"Onboarding ({tenant_id}): đã thấy API route trong access log nhưng chưa có contract. "
                        "Agent sẽ tự tìm openapi.json/openapi.yaml/swagger.json; nếu không có, hãy cung cấp tài liệu OpenAPI/Swagger."
                    )
        except Exception as exc:  # noqa: BLE001 — question is best-effort
            logger.debug("onboarding_pipeline: API contract question check failed tenant=%s err=%s", tenant_id, exc)
    if question is None:
        return
    await _open_question(ctx, tenant_id, question)


async def _open_question(ctx: WorkerHandlerContext, tenant_id: str, text: str) -> str:
    question_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    record = {"question_id": question_id, "tenant_id": tenant_id, "created_at": now, "resolved_at": None, "text": text, "channel": "telegram"}
    try:
        await ctx.redis.hset(dd.QUESTIONS_KEY.format(tenant_id=tenant_id), question_id, json.dumps(record, ensure_ascii=False))
        await ctx.redis.zadd(dd.QUESTIONS_OPEN_KEY.format(tenant_id=tenant_id), {question_id: now})
    except Exception as exc:  # noqa: BLE001
        logger.warning("onboarding_pipeline: open_question store failed tenant=%s err=%s", tenant_id, exc)
        return question_id

    chat_id = await _resolve_tenant_chat_id(ctx, tenant_id)
    if chat_id is not None and ctx.telegram is not None:
        try:
            await ctx.telegram.send_message(chat_id, text)
        except Exception as exc:  # noqa: BLE001 — Telegram send is best-effort
            logger.warning("onboarding_pipeline: telegram send failed tenant=%s err=%s", tenant_id, exc)
    return question_id


async def resolve_question(ctx: WorkerHandlerContext, tenant_id: str, question_id: str) -> bool:
    """Mark an open question resolved (e.g. tenant replied) — removes it from the open set."""
    raw = await ctx.redis.hget(dd.QUESTIONS_KEY.format(tenant_id=tenant_id), question_id)
    if not raw:
        return False
    try:
        record = json.loads(raw)
    except Exception:
        return False
    record["resolved_at"] = int(time.time())
    await ctx.redis.hset(dd.QUESTIONS_KEY.format(tenant_id=tenant_id), question_id, json.dumps(record, ensure_ascii=False))
    await ctx.redis.zrem(dd.QUESTIONS_OPEN_KEY.format(tenant_id=tenant_id), question_id)
    return True


async def _resolve_tenant_chat_id(ctx: WorkerHandlerContext, tenant_id: str) -> int | None:
    admin_repo = getattr(ctx, "admin_repo", None)
    if admin_repo is None:
        return None
    try:
        return await admin_repo.get_tenant_telegram_chat_id(tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("onboarding_pipeline: chat_id lookup failed tenant=%s err=%s", tenant_id, exc)
        return None


async def recompute_readiness(ctx: WorkerHandlerContext, tenant_id: str) -> dict[str, Any] | None:
    """A6/A7: recompute the readiness checklist + persist (Postgres source-of-truth,
    Redis write-through cache via AdminConfigRepo.set_tenant_readiness)."""
    admin_repo = getattr(ctx, "admin_repo", None)
    if admin_repo is None:
        return None
    fields = await dd.compute_readiness(ctx.redis, admin_repo, tenant_id)
    try:
        return await admin_repo.set_tenant_readiness(tenant_id=tenant_id, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("onboarding_pipeline: readiness persist failed tenant=%s err=%s", tenant_id, exc)
        return None


async def _persist_onboarding_mission(
    ctx: WorkerHandlerContext, tenant_id: str, *, readiness: dict[str, Any] | None, probe: str,
) -> None:
    """Project discovery progress into the durable AOIP Mission read-model.

    This is an additive worker-side projection: the discovery document and PG
    readiness state remain their own sources of truth.  Missing PG readiness is
    represented as an active mission with zero completion, never as success.
    """
    from aoip.mission import Mission, MissionState
    from aoip.mission_store import MissionStore

    ready = bool(readiness and readiness.get("readiness_flag"))
    mission = Mission(
        mission_id=f"onboarding:{tenant_id}", goal="onboard_tenant", scope=tenant_id,
    ).to(MissionState.PLANNED).to(MissionState.ASSIGNED).to(MissionState.IN_PROGRESS)
    completion = 1.0 if ready else 0.0
    state = MissionState.COMPLETED if ready else MissionState.IN_PROGRESS
    from dataclasses import replace
    mission = replace(mission, state=state, completion=completion,
                      dod_passed=("readiness_gate",) if ready else (),
                      dod_failed=() if ready else ("readiness_gate",))
    try:
        await MissionStore(ctx.redis).save(
            tenant_id, mission, last_activity=f"discovery probe: {probe}",
            next_action=None if ready else "collect remaining onboarding evidence",
        )
    except Exception as exc:  # noqa: BLE001 — projection cannot drop evidence
        logger.warning("onboarding_pipeline: mission projection failed tenant=%s err=%s", tenant_id, exc)
