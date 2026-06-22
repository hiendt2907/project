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
    discovery_data = fact.get("discovery_data") if isinstance(fact, dict) else None
    if not isinstance(discovery_data, dict):
        logger.warning("onboarding_pipeline: empty discovery_data probe=%s tenant=%s", probe, tenant_id)
        return

    try:
        await dd.accumulate_probe_fact(ctx.redis, tenant_id, probe, discovery_data)
    except Exception as exc:  # noqa: BLE001 — accumulation is best-effort, never blocks consumer
        logger.warning("onboarding_pipeline: accumulate failed tenant=%s probe=%s err=%s", tenant_id, probe, exc)
        return

    if trace:
        await mark_stage(ctx.redis, trace, "EVIDENCE", "ok", detail=f"onboarding_accumulated probe={probe}", lane="ONBOARDING_DISCOVERY")

    await _detect_gaps_and_ask(ctx, tenant_id, probe, discovery_data)
    try:
        await dd.regenerate_diagrams(ctx.redis, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("onboarding_pipeline: diagram regen failed tenant=%s err=%s", tenant_id, exc)
    await recompute_readiness(ctx, tenant_id)


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
    """A5: detect an obvious gap in this probe's data, ask the tenant via Telegram."""
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
