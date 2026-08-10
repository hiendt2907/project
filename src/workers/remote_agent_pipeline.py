"""Remote agent processing pipeline — orchestrates Stages 2-6 for RemoteAgent evidence.

Called from evidence_consumer.reason_from_diagnostic_evidence when
ev_doc["evidence_source"] == "RemoteAgent".

Pipeline:
  Stage 2 — Cluster (upsert_cluster)
  Stage 3 — Triage (triage_cluster: RAG lookup + urgency)
  Stage 4 — Research (analyze_cluster: LLM, only for UNKNOWN_RESEARCH)
  Stage 5 — Learn (write_lessons: write to RAG)
  Stage 6 — Notify (Telegram for critical/high)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from anomaly.remote_host_baseline import REMOTE_Z_THRESHOLD
from pkg.domain.taxonomy import UNKNOWN as _DOMAIN_UNKNOWN, normalize_domain
from pkg.reasoning.domain_signals import detect_domain
from pkg.reasoning.evidence_cluster import upsert_cluster
from pkg.reasoning.evidence_fingerprint import fingerprint_evidence
from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.signer import AuditLedgerError
from workers.handler_context import WorkerHandlerContext
from workers.remote_advisor import analyze_cluster
from workers.remote_diagnostic_archiver import write_lessons
from workers.remote_triage import quick_urgency_no_rag, triage_cluster
from workers.telegram_advisory_emitter import render_advisory_to_telegram
from workers.remote_diagnosis_emitter import (
    diagnosis_has_real_finding,
    emit_diagnosis_to_telegram,
    has_placeholder_parroting,
)
from workers.pipeline_stages import mark_stage
from workers.trace_context import inbound_trace_scope

logger = logging.getLogger(__name__)

# Giữ strong reference tới background task để GC không thu hồi giữa chừng và
# exception không bị nuốt silently (CRAT write trong _run_diagnosis_and_notify).
_BG_DIAG_TASKS: set[asyncio.Task] = set()


def _track_bg_task(task: asyncio.Task) -> None:
    _BG_DIAG_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BG_DIAG_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "[RAP] background diagnosis task failed: %s", t.get_name(),
                exc_info=t.exception(),
            )

    task.add_done_callback(_on_done)

def _domain_or_empty(value: Any) -> str:
    """Domain canonical, hoặc RỖNG nếu không nhận ra.

    Cố ý không trả ``unknown``: rỗng còn được call site sau lấp
    (last-non-empty-wins trong ``mark_stage``), còn ``unknown`` thì đứng
    nguyên và hiện lên portal như một lĩnh vực có thật.
    """
    d = normalize_domain(value if isinstance(value, str) else None)
    return "" if d == _DOMAIN_UNKNOWN else d


_NOTIFY_TIERS = frozenset({"critical", "high"})
_RESEARCH_ROUTES = frozenset({"UNKNOWN_RESEARCH"})

_DISCOVERY_EVIDENCE_TOPIC_DEFAULT = "omni-discovery-evidence"

# Side-channel for healthy remote_system_metrics heartbeat samples — kept out of
# Active Traces (no mark_stage), readable via GET /agents/remote/{agent_id}/baseline.
_BASELINE_OK_PREFIX = "omni:remote_agent:baseline_ok:"
_BASELINE_OK_TTL_SEC = 600

# Side-channel for "all logs clean" heartbeat samples from remote_log_errors probe.
_LOG_BASELINE_PREFIX = "omni:remote_agent:log_baseline:"
_LOG_BASELINE_TTL_SEC = 300


async def _store_healthy_baseline_sample(
    redis: Any, agent_id: str, fact: dict[str, Any], zscores: dict[str, float]
) -> None:
    try:
        import time

        snapshot = {"ts": int(time.time()), "fact": fact, **zscores}
        await redis.set(
            f"{_BASELINE_OK_PREFIX}{agent_id}", json.dumps(snapshot), ex=_BASELINE_OK_TTL_SEC
        )
    except Exception as exc:  # noqa: BLE001 — side-channel write is best-effort
        logger.warning("[RAP] store_healthy_baseline_sample failed agent=%s err=%r", agent_id, exc)


async def _store_log_baseline_sample(redis: Any, agent_id: str, extracted: dict[str, Any]) -> None:
    try:
        import time

        snapshot = {"ts": int(time.time()), "fact": extracted}
        await redis.set(
            f"{_LOG_BASELINE_PREFIX}{agent_id}", json.dumps(snapshot), ex=_LOG_BASELINE_TTL_SEC
        )
    except Exception as exc:  # noqa: BLE001 — side-channel write is best-effort
        logger.warning("[RAP] store_log_baseline_sample failed agent=%s err=%r", agent_id, exc)


async def handle_discovery_evidence(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    trace: str,
) -> str:
    """Validate + forward onboarding discovery evidence (process_list/port_scan/
    service_topology/doc-snapshot probes) to the onboarding worker via Kafka.

    Stub for step-2 of the onboarding-ops-agent plan: no diagnosis/clustering
    here, this is NOT an alert — the onboarding worker (step-3) consumes
    ``omni-discovery-evidence`` to build per-tenant documentation + Mermaid
    diagrams. tenant_id comes from the agent's own config (threaded through
    coerce_evidence_dict), never inferred by an LLM.
    """
    tenant_id = str(ev_doc.get("tenant_id") or "default")
    probe = str(ev_doc.get("probe") or "unknown")

    kafka = getattr(ctx, "kafka", None)
    if kafka is None:
        logger.warning("[RAP] discovery_evidence dropped trace=%s — no kafka bus", trace)
        await mark_stage(ctx.redis, trace, "DISPATCH", "fail", detail="no_kafka_bus", signal_kind="learning")
        return ""

    topic = getattr(ctx.settings, "kafka_topic_discovery_evidence", _DISCOVERY_EVIDENCE_TOPIC_DEFAULT)
    await mark_stage(ctx.redis, trace, "EVIDENCE", "ok", detail=f"probe={probe} tenant={tenant_id}", signal_kind="learning")
    try:
        await kafka.send_dict(
            topic,
            {"data": json.dumps(ev_doc, ensure_ascii=False)},
            key=trace.encode("utf-8", errors="ignore"),
        )
    except Exception as exc:
        logger.warning("[RAP] discovery_evidence kafka send failed trace=%s err=%s", trace, exc)
        await mark_stage(ctx.redis, trace, "DISPATCH", "fail", detail=f"kafka_send_failed: {exc}", signal_kind="learning")
        return ""

    await mark_stage(ctx.redis, trace, "DISPATCH", "ok", detail=f"forwarded topic={topic}", signal_kind="learning")
    logger.info(
        "[RAP] discovery_evidence forwarded trace=%s tenant=%s probe=%s topic=%s",
        trace, tenant_id, probe, topic,
    )
    return ""


async def handle_remote_agent_evidence(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    trace: str,
) -> str:
    """End-to-end pipeline for a single remote-agent evidence item."""
    logger.info("[RAP]Handling remote agent evidence trace=%s probe=%s", trace, ev_doc.get("probe"))
    extracted = ev_doc.get("extracted_fact") or {}
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except Exception:
            extracted = {}
    if not isinstance(extracted, dict):
        extracted = {}
    agent_id = str(extracted.get("agent_id") or "unknown-agent")
    probe = str(ev_doc.get("probe") or "unknown")
    alert_hint = str(ev_doc.get("alert_hint") or "")
    raw = str(ev_doc.get("raw") or "")
    # Domain nguồn TỰ KHAI, dùng cho các mark_stage chạy TRƯỚC detect_domain().
    # Không nhận ra ⇒ rỗng (không ghi "unknown"): rỗng còn được lấp về sau.
    _early_domain = _domain_or_empty(ev_doc.get("domain"))
    labels = {
        "alertname": str(ev_doc.get("alert_rule") or ""),
        "namespace": str(ev_doc.get("namespace") or ""),
        "evidence_source": str(ev_doc.get("evidence_source") or ""),
    }

    # ── Lane 1 (resource) baseline for remote hosts ───────────────────────
    # Prometheus cannot scrape customer servers, so the in-cluster 3σ engine is
    # blind to them. Feed agent-reported cpu/mem/disk into a per-host rolling
    # baseline and stamp z-scores onto the evidence so the resource lane gets a
    # real "normal for THIS host" signal instead of a static threshold.
    result = str(ev_doc.get("result") or "PASSED")
    zscores: dict[str, float] = {}
    if probe == "remote_system_metrics":
        try:
            from anomaly.remote_host_baseline import update_remote_host_baseline

            host = str(ev_doc.get("namespace") or "") or agent_id
            tenant_id = str(ev_doc.get("tenant_id") or "default")
            zscores = await update_remote_host_baseline(
                ctx.redis, tenant_id=tenant_id, host=host, fact=extracted
            )
            if zscores:
                # Enrich a copy, never mutate the caller's extracted_fact in place.
                extracted = {**extracted, **zscores}
                ev_doc = {**ev_doc, "extracted_fact": extracted}
        except Exception as exc:  # noqa: BLE001 — baseline is best-effort
            logger.warning("[RAP] remote_host_baseline failed trace=%s err=%r", trace, exc)

        # Heartbeat sample (threshold not breached, no 3σ baseline breach) carries
        # no diagnosis signal — feeding it through cluster/triage/mark_stage just
        # spams Active Traces with one "healthy" entry per host per collect cycle.
        # Park it in a side-channel snapshot instead; only a real breach proceeds
        # into the pipeline below.
        # REMOTE_Z_THRESHOLD, không phải literal 3.0: đây là hằng số đã có nguồn chuẩn
        # (three_sigma.DEFAULT_THRESHOLD) với comment "không phát minh số mới cho remote
        # host" — trước đây bị gõ lại thành literal ở đây, hai giá trị trùng nhau hôm nay
        # nhưng sẽ trôi khỏi nhau âm thầm nếu một bên được tune sau này (audit 2026-08-10
        # #7, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md).
        is_anomalous = result == "FAILED" or any(
            abs(v) > REMOTE_Z_THRESHOLD for v in zscores.values()
        )
        if not is_anomalous:
            await _store_healthy_baseline_sample(ctx.redis, agent_id, extracted, zscores)
            return ""

    # ── Container log PASSED (no surge) — park in side-channel, skip pipeline ──
    # A clean log scan has no diagnosis signal; feeding it through cluster/triage
    # just floods Active Traces with one "healthy" entry per host per collect cycle.
    if probe == "remote_log_errors" and result == "PASSED":
        await _store_log_baseline_sample(ctx.redis, agent_id, extracted)
        return ""

    # ── Stage 2: Cluster ──────────────────────────────────────────────────
    fp = fingerprint_evidence({"probe": probe, "result": result, "alert_hint": alert_hint, "raw": raw})
    # `domain_hint` = domain COLLECTOR TỰ KHAI (Phase 1). Bỏ nó đi là để suy đoán ghi
    # đè nguồn: đã trả giá 2026-07-30 — `remote_log_errors` (collectors/logs.py khai
    # `application`) bị cascade nội dung suy thành `kubernetes`, nên sự cố ứng dụng
    # trên host khách bị gán sai lĩnh vực và gọi sai bộ chẩn đoán.
    # `envelope.lane` (trục A) chỉ còn là HÀNG CHÓT của cascade và chỉ tồn tại để
    # đọc payload từ agent bản cũ — không còn được lưu/hiển thị ở đâu nữa.
    _legacy_envelope_lane = str(ev_doc.get("lane") or "")
    domain = detect_domain(
        probe, alert_hint, raw, _legacy_envelope_lane, labels=labels,
        domain_hint=ev_doc.get("domain"),
    )

    try:
        cluster = await upsert_cluster(ctx.redis, agent_id, fp, ev_doc, domain)
    except Exception as exc:
        logger.warning("[RAP] cluster_upsert_failed trace=%s err=%s", trace, exc)
        await mark_stage(ctx.redis, trace, "EVIDENCE", "fail", detail=f"cluster_upsert_failed: {exc}", domain=_early_domain, signal_kind="diagnostic")
        return ""

    # ── Repeat-cluster fast-path — skip RAG triage for non-urgent repeats ───
    # An ongoing condition that fired last cycle (is_new=False) only needs a
    # cheap urgency re-assessment; if urgency hasn't escalated to notify tier
    # we exit here without a RAG round-trip or an Active Trace entry.
    # Critical/high signals still proceed to full triage so escalation is
    # detected and RAG playbook lookup runs.
    if not cluster.is_new:
        fast_urgency = quick_urgency_no_rag(cluster)
        if fast_urgency not in _NOTIFY_TIERS:
            logger.debug(
                "[RAP] repeat_suppressed_fast fp=%s domain=%s urgency=%s count=%d",
                fp, domain, fast_urgency, cluster.count,
            )
            return f"remote_agent:repeat_suppressed:{fast_urgency}"

    logger.info(
        "[RAP] cluster fp=%s domain=%s count=%d is_new=%s is_storm=%s",
        fp, domain, cluster.count, cluster.is_new, cluster.is_storm,
    )

    # ── Stage 3: Triage ───────────────────────────────────────────────────
    triage = await triage_cluster(ctx, cluster)

    # Safety net: repeat cluster whose fast-path estimated high/critical but
    # RAG triage downgraded it (e.g. known-normal pattern) — no Active Trace.
    if not cluster.is_new and triage.urgency not in _NOTIFY_TIERS:
        logger.debug(
            "[RAP] repeat_cluster_suppressed fp=%s count=%d urgency=%s route=%s — no new trace",
            fp, cluster.count, triage.urgency, triage.route,
        )
        return f"remote_agent:{triage.route}:repeat_suppressed"

    # Điểm SỚM NHẤT của đường remote-agent mà domain đã có (ngay sau detect_domain).
    # `domain` last-non-empty-wins trong meta ⇒ khai một lần ở đây là cả trace có
    # domain, không cần rải vào mọi mark_stage phía sau.
    await mark_stage(
        ctx.redis, trace, "EVIDENCE", "ok",
        detail=f"remote agent={agent_id} domain={domain} probe={probe}",
        domain=domain, signal_kind="diagnostic",
    )

    logger.info(
        "[RAP] triage fp=%s route=%s urgency=%s",
        fp, triage.route, triage.urgency,
    )
    # RAG stage reflects the playbook recall done inside triage.
    logger.info("[RAP] About to process RAG stage for trace=%s", trace)
    try:
        recall_obj = getattr(triage, "recall", None)
        logger.info("[RAP] Recall object for trace=%s: %s", trace, recall_obj)
        if recall_obj is not None:
            _recall_score = getattr(recall_obj, "top_score", None)
            logger.info("[RAP] Recall score for trace=%s: %s", trace, _recall_score)
        else:
            _recall_score = None
            logger.info("[RAP] Recall object is None for trace=%s", trace)

        if _recall_score:
            logger.info("[RAP] Marking RAG stage as ok for trace=%s with score=%s", trace, _recall_score)
            await mark_stage(ctx.redis, trace, "RAG", "ok", detail=f"recall={_recall_score:.3f} route={triage.route}", domain=_early_domain, signal_kind="diagnostic")
            logger.info("[RAP] Successfully marked RAG stage as ok for trace=%s", trace)
        else:
            logger.info("[RAP] Marking RAG stage as skip for trace=%s (no recall score)", trace)
            await mark_stage(ctx.redis, trace, "RAG", "skip", detail=f"no_hit route={triage.route}", domain=_early_domain, signal_kind="diagnostic")
            logger.info("[RAP] Successfully marked RAG stage as skip for trace=%s", trace)
    except Exception as e:
        logger.error("[RAP] Error processing RAG stage for trace=%s: %s", trace, str(e), exc_info=True)
        # Re-raise to see if it bubbles up
        raise

    # ── Stage 4: Research — multi-turn diagnosis loop for urgent clusters ──
    # INVARIANT INV_NO_SINGLE_TURN: diagnosis loop runs minimum 2 turns.
    # Runs as background asyncio.Task so Kafka consumer is not blocked.
    #
    # Routing decision:
    #   UNKNOWN_RESEARCH + critical/high  → background multi-turn diagnosis loop
    #   KNOWN_BASELINE   + critical/high  → still diagnose (known pattern but urgent)
    #   any route        + medium/below   → fallback single-turn advisory
    advisory = None
    diag_task_launched = False
    # KNOWN_WITH_FIX may match K8s playbooks that are semantically wrong for
    # remote-agent evidence (disk/storage domain vs k8s executor tools).
    # Treat it identically to KNOWN_BASELINE for the research decision.
    needs_research = triage.route in _RESEARCH_ROUTES or (
        triage.route in ("KNOWN_BASELINE", "KNOWN_WITH_FIX") and triage.urgency in _NOTIFY_TIERS
    )
    if needs_research and triage.urgency in _NOTIFY_TIERS:
        chat_id = getattr(ctx, "telegram_chat_id", None) or getattr(
            ctx.settings, "telegram_admin_chat_id", None
        )
        llm = getattr(ctx, "llm", None)
        model = getattr(getattr(ctx, "settings", None), "diag_evidence_llm_model", None) or "qwen3:8b"
        num_ctx = int(getattr(getattr(ctx, "settings", None), "llm_num_ctx", 8192) or 8192)

        if llm is not None and chat_id is not None:
            await mark_stage(ctx.redis, trace, "LLM", "ok", detail="multi-turn diagnosis loop launched", domain=_early_domain, signal_kind="diagnostic")
            _track_bg_task(asyncio.create_task(
                _run_diagnosis_and_notify(
                    ctx=ctx,
                    ev_doc=ev_doc,
                    agent_id=agent_id,
                    trace=trace,
                    llm=llm,
                    model=model,
                    num_ctx=num_ctx,
                    chat_id=int(chat_id),
                ),
                name=f"diag-{trace[:12]}",
            ))
            diag_task_launched = True
            logger.info("[RAP] diagnosis_loop launched as background task trace=%s", trace)
        else:
            await mark_stage(ctx.redis, trace, "LLM", "ok", detail="single-turn advisory", domain=_early_domain, signal_kind="diagnostic")
            advisory = await analyze_cluster(ctx, cluster, recall=triage.recall)
    elif triage.route in _RESEARCH_ROUTES:
        await mark_stage(ctx.redis, trace, "LLM", "ok", detail="single-turn advisory", domain=_early_domain, signal_kind="diagnostic")
        advisory = await analyze_cluster(ctx, cluster, recall=triage.recall)
    else:
        await mark_stage(ctx.redis, trace, "LLM", "skip", detail=f"route={triage.route} urgency={triage.urgency} — no advisory", domain=_early_domain, signal_kind="diagnostic")

    if advisory is not None:
        _v = getattr(advisory, "verdict", "") or ""
        await mark_stage(ctx.redis, trace, "SCHEMA", "ok", detail=f"verdict={_v}", domain=_early_domain, signal_kind="diagnostic")
        # Persist the advisory per trace so the UI can surface verification_steps,
        # impact_chain, remediation and forecast (deep-check report).
        try:
            await _persist_trace_advisory(ctx.redis, trace, advisory, domain)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("[RAP] persist_advisory failed trace=%s err=%r", trace, exc)
    elif not diag_task_launched:
        await mark_stage(ctx.redis, trace, "SCHEMA", "skip", detail="no advisory produced", domain=_early_domain, signal_kind="diagnostic")

    # ── Stage 5: Learn — write to RAG ────────────────────────────────────
    await write_lessons(ctx, cluster, triage, advisory)

    # ── Stage 6: Notify — Telegram for fallback advisory (non-loop path) ──
    if advisory is not None and triage.urgency in _NOTIFY_TIERS:
        try:
            chat_id = getattr(ctx, "telegram_chat_id", None) or getattr(
                ctx.settings, "telegram_admin_chat_id", None
            )
            if chat_id is not None:
                import dataclasses
                if not hasattr(advisory, "trace_id") or not advisory.trace_id:
                    advisory = dataclasses.replace(advisory, trace_id=trace)
                await render_advisory_to_telegram(ctx, advisory, int(chat_id))
                await mark_stage(ctx.redis, trace, "DISPATCH", "ok", detail="SUGGEST_REMEDIATION (Telegram)", domain=_early_domain, signal_kind="diagnostic")
        except Exception as exc:
            logger.warning("[RAP] telegram_notify_failed trace=%s err=%s", trace, exc)
            await mark_stage(ctx.redis, trace, "DISPATCH", "fail", detail=f"telegram_notify_failed: {exc}", domain=_early_domain, signal_kind="diagnostic")
    elif advisory is not None:
        await mark_stage(ctx.redis, trace, "DISPATCH", "skip", detail=f"urgency={triage.urgency} — below notify tier", domain=_early_domain, signal_kind="diagnostic")

    verdict = advisory.verdict if advisory else ("diagnosis_loop_launched" if diag_task_launched else "no_advisory")
    logger.info(
        "[RAP] done trace=%s fp=%s route=%s urgency=%s verdict=%s",
        trace, fp, triage.route, triage.urgency, verdict,
    )
    return f"remote_agent:{triage.route}:{verdict}"


_TRACE_ADVISORY_KEY = "omni:trace:advisory:"
_TRACE_ADVISORY_TTL = 3600


def _advisory_to_dict(advisory: Any) -> dict[str, Any]:
    """Best-effort serialize an advisory (pydantic model or dataclass) to a dict."""
    if hasattr(advisory, "model_dump"):
        try:
            return advisory.model_dump(mode="json")
        except Exception:
            pass
    import dataclasses as _dc
    if _dc.is_dataclass(advisory):
        try:
            return _dc.asdict(advisory)
        except Exception:
            pass
    # Fallback: pull known fields off the object.
    out: dict[str, Any] = {}
    for f in ("verdict", "root_cause", "confidence", "affected_workload",
              "verification_steps", "proposed_remediation", "forecast", "impact_chain"):
        v = getattr(advisory, f, None)
        if v is not None:
            out[f] = v if isinstance(v, (str, int, float, list, dict)) else str(v)
    return out


async def _persist_trace_advisory(redis: Any, trace: str, advisory: Any, domain: str) -> None:
    """Store the advisory JSON at omni:trace:advisory:{trace} for the UI deep-check panel."""
    if redis is None or not trace:
        return
    doc = {"trace_id": trace, "domain": domain, "advisory": _advisory_to_dict(advisory)}
    await redis.setex(f"{_TRACE_ADVISORY_KEY}{trace}", _TRACE_ADVISORY_TTL, json.dumps(doc, ensure_ascii=False, default=str))


async def _run_diagnosis_and_notify(
    ctx: WorkerHandlerContext,
    ev_doc: dict,
    agent_id: str,
    trace: str,
    llm: Any,
    model: str,
    num_ctx: int,
    chat_id: int,
) -> None:
    """Background task: run multi-turn diagnosis loop then emit Telegram.

    INVARIANT INV_DIAG_STORED: session must be stored in Redis before emit.

    Trace binding (2026-08-02): mọi thứ trong đây chạy trong một asyncio task
    RIÊNG. Instrument LLM đo được 63/67 lệnh gọi ghi ``trace=-``, và chỗ mất
    nằm đúng ở nhánh này — không có trace id thì không nối được một lệnh gọi
    LLM với sự cố sinh ra nó, tức mất khả năng truy "chẩn đoán sai này đến từ
    prompt nào". Bind TẠI ĐÂY chứ không dựa vào context của caller: task này
    có thể được tạo từ nhiều đường, và một thay đổi ở chỗ tạo task không được
    phép âm thầm làm rơi trace lần nữa.
    """
    async with inbound_trace_scope(trace):
        await _run_diagnosis_and_notify_inner(
            ctx=ctx, ev_doc=ev_doc, agent_id=agent_id, trace=trace,
            llm=llm, model=model, num_ctx=num_ctx, chat_id=chat_id,
        )


async def _run_diagnosis_and_notify_inner(
    ctx: WorkerHandlerContext,
    ev_doc: dict,
    agent_id: str,
    trace: str,
    llm: Any,
    model: str,
    num_ctx: int,
    chat_id: int,
) -> None:
    """Thân thật của `_run_diagnosis_and_notify` — luôn chạy trong trace scope."""
    from services.analyst.diagnosis_loop import run_diagnosis_loop
    from workers.archivist import recall_knowledge_context

    _early_domain = _domain_or_empty(ev_doc.get("domain"))
    tenant_id = str(ev_doc.get("tenant_id") or ev_doc.get("tenant") or "default")

    # D3 (2026-07-31): nạp kiến thức RAG (SOP + kinh nghiệm) để NHÉT vào prompt chẩn
    # đoán. Trước đây prompt không có RAG ⇒ LLM chẩn lại từ 0 mỗi lần. Best-effort:
    # lỗi RAG không được chặn đường chẩn đoán.
    knowledge_text = ""
    try:
        _query = (
            f"domain={ev_doc.get('domain','')} probe={ev_doc.get('probe','')} "
            f"{str(ev_doc.get('alert_hint') or '')}"
        )
        knowledge_text = await recall_knowledge_context(
            ctx, query_text=_query, tenant_id=tenant_id,
        )
    except Exception as _kerr:
        logger.debug("[RAP] knowledge_ctx err trace=%s err=%r", trace, _kerr)

    try:
        session = await run_diagnosis_loop(
            redis=ctx.redis,
            llm_client=llm,
            agent_id=agent_id,
            ev_doc=ev_doc,
            trace_id=trace,
            model=model,
            num_ctx=num_ctx,
            knowledge_text=knowledge_text,
            # Xếp hàng qua làn reactive: đo thật 2026-08-02 cho thấy 18 phiên
            # 8-lượt chồng nhau trong 14 phút cùng đập vào một model 7B mà
            # KHÔNG có gate nào — `acquire_reactive()` từng không có call site
            # nào trong toàn repo dù làn này đã được cấp slot sẵn.
            semaphore=getattr(ctx, "semaphore", None),
        )
        _turns = getattr(session, "total_turns", None)
        if _turns is None and isinstance(session, dict):
            _turns = session.get("total_turns")
        await mark_stage(ctx.redis, trace, "SCHEMA", "ok", detail=f"diagnosis session stored turns={_turns}", domain=_early_domain, signal_kind="diagnostic")

        # CRAT fail-closed (AGENTS.md INVARIANT: write_audit_block() MUST succeed
        # before Telegram emit / action dispatch — applies to this lane too, not
        # only the K8s/advisory lane).
        _final = session.get("final") if isinstance(session, dict) else {}

        # B1+B4 (2026-07-31): KHÔNG phát thẻ báo động đỏ khi chẩn đoán không có thực chất
        # — LLM kết luận "hoạt động bình thường", hoặc nhại placeholder của prompt. Session
        # ĐÃ lưu ở Redis (SCHEMA stage) nên UI /diagnostics vẫn xem được; chỉ chặn cái thẻ
        # Telegram gây nhiễu (đo thật: user nhận 3 thẻ, 2 là rác loại này).
        if has_placeholder_parroting(_final) or not diagnosis_has_real_finding(_final):
            logger.info(
                "[RAP] suppress alarm trace=%s reason=%s rc=%r",
                trace,
                "placeholder" if has_placeholder_parroting(_final) else "no_real_finding",
                str((_final or {}).get("root_cause"))[:80],
            )
            await mark_stage(
                ctx.redis, trace, "DISPATCH", "skip",
                detail="no real finding — observed, not alarmed", domain=_early_domain, signal_kind="diagnostic",
            )
            return

        audit_payload = {
            "agent_id": agent_id,
            "probe": ev_doc.get("probe", ""),
            "domain": _early_domain,
            "root_cause": (_final or {}).get("root_cause", ""),
            "confidence": (_final or {}).get("confidence", 0.0),
            "affected_components": (_final or {}).get("affected_components", []),
            "total_turns": _turns,
            "degraded": session.get("degraded") if isinstance(session, dict) else None,
            # INV_DIAG_MEASURED: nếu cổng đo-lường đã vô hiệu hoá kết luận, dấu vết
            # đó phải nằm trong chuỗi audit — không chỉ trong log. Người đọc lại
            # phải biết thẻ đó đã bị hạ độ tin vì lý do gì.
            "unmeasured_quantities": (_final or {}).get("unmeasured_quantities", []),
            "contradicted_claims": (_final or {}).get("contradicted_claims", []),
            "confidence_inflation": (_final or {}).get("confidence_inflation"),
        }
        try:
            await write_audit_block(
                event_type="ADVISORY_DECISION",
                trace_id=trace,
                payload=audit_payload,
                redis=ctx.redis,
                kafka=ctx.kafka,
                kafka_topic=getattr(ctx.settings, "kafka_topic_audit_chain", "omni-audit-chain"),
                tenant_id=tenant_id,
            )
        except AuditLedgerError as _audit_err:
            logger.critical(
                "event=audit_chain_write_failed phase=remote_agent_diagnosis trace=%s err=%s FAIL_CLOSED",
                trace, _audit_err,
            )
            await mark_stage(ctx.redis, trace, "CRAT", "fail", detail="audit_chain_write_failed", domain=_early_domain, signal_kind="diagnostic")
            return  # fail-closed: do NOT emit Telegram without a successful audit block

        await mark_stage(ctx.redis, trace, "CRAT", "ok", detail="diagnosis audit block written", domain=_early_domain, signal_kind="diagnostic")
        await emit_diagnosis_to_telegram(ctx, session, chat_id, tenant_id=tenant_id)
        await mark_stage(ctx.redis, trace, "DISPATCH", "ok", detail="diagnosis emitted (Telegram)", domain=_early_domain, signal_kind="diagnostic")
        await _dispatch_auto_recovery_if_eligible(ctx, _final, agent_id, tenant_id, trace, _early_domain)
    except RuntimeError as exc:
        # INV_DIAG_STORED violated — do NOT emit Telegram
        logger.error("[RAP] diagnosis_aborted INV_DIAG_STORED trace=%s err=%s", trace, exc)
        await mark_stage(ctx.redis, trace, "SCHEMA", "fail", detail=f"diagnosis aborted: {exc}", domain=_early_domain, signal_kind="diagnostic")
    except Exception as exc:
        logger.error("[RAP] diagnosis_loop_error trace=%s err=%s", trace, exc)
        await mark_stage(ctx.redis, trace, "LLM", "fail", detail=f"diagnosis_loop_error: {exc}", domain=_early_domain, signal_kind="diagnostic")


async def _dispatch_auto_recovery_if_eligible(
    ctx: WorkerHandlerContext,
    final: dict | None,
    agent_id: str,
    tenant_id: str,
    trace: str,
    domain: str,
) -> None:
    """Phase 4 (0-6 roadmap): the closed loop's last hop. Runs AFTER CRAT and
    Telegram emit succeed — auto-recovery is best-effort on top of an already
    fully-recorded/notified diagnosis, never a precondition for either.

    Deliberately swallows all exceptions: a dispatch failure (network, gateway
    down, malformed advisory) must never be mistaken for a diagnosis failure —
    the diagnosis itself already succeeded and was already reported.
    """
    import httpx

    from workers.auto_recovery_bridge import dispatch_if_eligible

    try:
        async with httpx.AsyncClient() as client:
            result = await dispatch_if_eligible(
                settings=ctx.settings,
                http_client=client,
                final=final or {},
                agent_id=agent_id,
                tenant_id=tenant_id,
                trace_id=trace,
                # BẮT BUỘC. `auto_recovery_bridge` nay ghi CRAT TRƯỚC khi dispatch
                # (ledger hỏng ⇒ không phát lệnh, giữ đúng bất biến CRAT fail-closed)
                # và đăng ký lệnh vào work-list cho `remote_command_outcome_loop`.
                # Thiếu hai tham số này thì nó luôn trả `audit_ledger_unavailable`:
                # an toàn, nhưng toàn bộ đường tự khắc phục nằm im.
                redis=ctx.redis,
                kafka=ctx.kafka,
            )
    except Exception as exc:
        logger.error("[RAP] auto_recovery_dispatch_error trace=%s err=%s", trace, exc)
        await mark_stage(ctx.redis, trace, "AUTO_RECOVERY", "fail", detail=f"dispatch_error: {exc}", domain=domain, signal_kind="diagnostic")
        return

    # Lý do "không đủ điều kiện", KHÔNG phải lỗi. Trước đây nhánh này `return` câm —
    # 808/809 trace không ghi lấy một dòng, nên "vì sao không tự khắc phục" là câu
    # không trả lời được từ sổ. Nay vẫn không ghi `fail` (nó bóp méo tỉ lệ lỗi),
    # nhưng ghi `skip` KÈM LÝ DO. `agent_not_in_lab_allowlist` thuộc nhóm này:
    # agent ngoài allowlist là cấu hình có chủ đích, không phải sự cố.
    _SKIP_REASONS = (
        "no_suggested_recovery",
        "confidence_below_threshold",
        "gateway_api_key_not_configured",
        "agent_not_in_lab_allowlist",
    )
    reason = str(result.get("reason") or "")
    if reason in _SKIP_REASONS:
        await mark_stage(
            ctx.redis, trace, "AUTO_RECOVERY", "skip",
            detail=f"reason={reason}", domain=domain, signal_kind="diagnostic",
        )
        return

    status = "ok" if result.get("dispatched") else "fail"
    await mark_stage(
        ctx.redis, trace, "AUTO_RECOVERY", status,
        detail=f"reason={result.get('reason')} command_id={result.get('command_id')} state={result.get('state')}",
        domain=domain, signal_kind="diagnostic",
    )
