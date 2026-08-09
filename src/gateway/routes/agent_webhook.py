"""Remote agent webhook — registration và evidence ingestion từ external Linux agents."""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.tenant_context import get_tenant_ctx, is_admin_ctx, require_agent_tenant
from pkg.reasoning.domain_signals import detect_domain
from pkg.reasoning.evidence_fingerprint import fingerprint_evidence
from pkg.reasoning.sanitize import sanitize_evidence_field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/agent", tags=["remote-agent"])

_REGISTRY_PREFIX = "omni:remote_agent:registry:"
_REGISTRY_TTL = 120  # seconds — agent must re-register within this window to stay alive
_MAX_EVIDENCE_BATCH = 50
_EPS_PREFIX = "omni:remote_agent:eps:"          # ZSET: score=epoch_ms, member=unique id
_METRICS_PREFIX = "omni:remote_agent:metrics:"   # JSON: latest system metrics snapshot
_LOGS_PREFIX = "omni:remote_agent:logs:"         # List: last 100 log evidence items
_LOG_MAX = 100
_EPS_WINDOW_MS = 60_000  # 60s rolling window

# ── Rate-limiter and dedup constants ─────────────────────────────────────────
_RL_PREFIX = "omni:evrl:"           # omni:evrl:{agent_id} — per-agent item count
_RL_PROBE_PREFIX = "omni:evrl:p:"   # omni:evrl:p:{agent_id}:{probe}:{result}
_RL_WINDOW_S = 60
_RL_AGENT_LIMIT = 200               # max items/min per agent across all probes
# OBSERVED có hạn mức cao hơn vì nó là dòng đo LIÊN TỤC (mỗi chu kỳ agent), không
# phải kết quả kiểm tra rời rạc. Đặt bằng PASSED sẽ chặn mất mẫu và làm baseline 3σ
# thiếu dữ liệu đúng lúc cần nhất — khi host đang biến động.
_RL_PROBE_LIMITS: dict[str, int] = {
    "FAILED": 30, "INCONCLUSIVE": 20, "PASSED": 20, "OBSERVED": 60,
}
_DEDUP_PREFIX = "omni:evdedup:"     # omni:evdedup:{agent_id}:{fingerprint}
_DEDUP_WINDOW_S = 300               # 5-min dedup window
_DEDUP_PASS_COUNT = 3               # allow first N occurrences; after that, skip Kafka
_STORM_THRESHOLD = 20               # >20 same fingerprint → log storm

# Clean (PASSED) probe checks never enter the diagnostic pipeline — there is
# nothing to diagnose. They land in a per-agent "last check" side-channel
# instead, so the agent's health is still inspectable without spamming the
# Kafka diagnostic-evidence topic / Active Traces dashboard with no-op traces.
_CHECKS_PREFIX = "omni:remote_agent:checks:"   # HASH: field=probe, value=JSON last-clean-check
_CHECKS_TTL = 600
# remote_system_metrics is a continuous data feed (3-sigma baseline input),
# not a pass/fail probe — it must always reach the pipeline regardless of result.
_ALWAYS_PIPELINE_PROBES = {"remote_system_metrics"}


# ─── Models ──────────────────────────────────────────────────────────────────

class AgentRegisterRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=256)
    version: str = Field(default="1.0.0", max_length=32)
    capabilities: list[str] = Field(default_factory=list)  # ["metrics", "logs", "k8s"]
    adapter_domains: list[str] = Field(default_factory=list, max_length=32)
    platform: str = Field(default="linux", max_length=64)
    k8s_namespace: str = Field(default="", max_length=256)
    tenant_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    # Self-reported LAN-facing IP (see remote_agent.emitter._detect_local_ip) —
    # preferred over request.client.host, which collapses to one shared NAT
    # egress IP when multiple agent hosts sit behind the same gateway.
    local_ip: str = Field(default="", max_length=64)
    # Self-hash of the running bundle (remote_agent.bundle_hash) — compared
    # against the published release manifest for drift detection (IT-2).
    bundle_sha256: str = Field(default="", max_length=64)
    # Self-hash of the aoip package, reported only by hosts running the
    # canonical AOIP employee runtime (IT-4). Legacy agents omit it and are
    # judged on bundle_sha256 alone — no false "drifted" during transition.
    aoip_bundle_sha256: str = Field(default="", max_length=64)


class EvidenceItem(BaseModel):
    trace_id: str = Field(min_length=1, max_length=128)
    probe: str = Field(min_length=1, max_length=128)
    alert_rule: str = Field(default="RemoteAgentAlert", max_length=256)
    alert_hint: str = Field(default="", max_length=2000)
    # OBSERVED (2026-07-30): mẫu đo được ghi nhận, agent KHÔNG phán xét. Khác PASSED
    # ở chỗ agent không khẳng định "đã kiểm và sạch" — nó chỉ báo số. Phải nằm trong
    # pattern này, nếu không gateway trả 422, emitter retry 4 lần rồi bỏ cả batch và
    # toàn bộ dòng METRIC_SAMPLE tắt lịm trong im lặng.
    result: str = Field(
        default="PASSED", pattern="^(PASSED|FAILED|INCONCLUSIVE|SKIPPED|OBSERVED)$"
    )
    extracted_fact: dict[str, Any] = Field(default_factory=dict)
    raw: str = Field(default="", max_length=4000)
    symptom_group: str = Field(default="", max_length=128)
    lane: str = Field(default="SYS_RESOURCE", max_length=64)
    # NON-AUTHORITATIVE sensor hint — Omni re-derives the proof lane on ingest.
    lane_hint: str = Field(default="", max_length=64)
    lane_authoritative: bool = Field(default=False)
    stream_tags: list[str] = Field(default_factory=list)
    namespace: str = Field(default="", max_length=256)
    ts: str = Field(default="")
    # Set by the agent per-probe (e.g. "DiscoveryEvidence" for onboarding probes).
    # Falls back to "RemoteAgent" — never trust this for tenant scoping.
    evidence_source: str = Field(default="RemoteAgent", max_length=64)
    # Signal routing: ANOMALY → omni-diagnostic-evidence; others → omni-knowledge-evidence.
    # INV_KNOWLEDGE_NOT_ALERT: non-ANOMALY signals MUST NOT enter the diagnostic pipeline.
    signal_type: str = Field(default="ANOMALY", max_length=32)


class AgentEvidenceRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=256)
    # Tenant comes from the agent's own config (OMNI_AGENT_TENANT_ID), not from
    # any LLM inference downstream — this is the source of truth for isolation.
    tenant_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    evidence: list[EvidenceItem] = Field(max_length=_MAX_EVIDENCE_BATCH)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _get_kafka(request: Request) -> Any:
    k = getattr(request.app.state, "kafka", None)
    if k is None:
        raise HTTPException(status_code=503, detail="Kafka not available")
    return k


def _get_evidence_topic(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_evidence", "omni-diagnostic-evidence")


def _get_knowledge_topic(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_knowledge_evidence", "omni-knowledge-evidence")


# ─── GIGO helpers ────────────────────────────────────────────────────────────

_INJECTION_PHRASES = [
    "ignore previous", "disregard the", "forget all instructions",
    "new instruction:", "override previous", "system:", "assistant:",
]


def _is_hard_blocked(item: EvidenceItem) -> tuple[bool, str]:
    """Return (True, reason) if this item should be dropped before Kafka."""
    if item.result == "SKIPPED":
        return True, "result_skipped_no_data"

    has_content = (
        bool(item.alert_hint.strip())
        or bool(item.raw.strip())
        or bool(item.extracted_fact)
    )
    if not has_content:
        return True, "empty_content_no_learning_value"

    combined = (item.alert_hint + " " + item.raw).lower()
    if any(phrase in combined for phrase in _INJECTION_PHRASES):
        return True, "prompt_injection_detected"

    return False, ""


async def _check_rate_limit(redis: Any, agent_id: str, item: EvidenceItem) -> bool:
    """Return True if item passes rate limits, False if it should be dropped (HTTP 429 at batch level)."""
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - _RL_WINDOW_S * 1000

    # Per-agent global limit
    agent_key = f"{_RL_PREFIX}{agent_id}"
    await redis.zadd(agent_key, {f"{now_ms}-{uuid.uuid4().hex[:8]}": now_ms})
    await redis.zremrangebyscore(agent_key, "-inf", cutoff_ms)
    await redis.expire(agent_key, _RL_WINDOW_S + 5)
    agent_count = await redis.zcard(agent_key)
    if agent_count > _RL_AGENT_LIMIT:
        return False

    # Per-probe-result limit
    probe_key = f"{_RL_PROBE_PREFIX}{agent_id}:{item.probe}:{item.result}"
    await redis.zadd(probe_key, {f"{now_ms}-{uuid.uuid4().hex[:8]}": now_ms})
    await redis.zremrangebyscore(probe_key, "-inf", cutoff_ms)
    await redis.expire(probe_key, _RL_WINDOW_S + 5)
    probe_count = await redis.zcard(probe_key)
    limit = _RL_PROBE_LIMITS.get(item.result, 20)
    if probe_count > limit:
        return False

    return True


def _is_clean_check(item: EvidenceItem) -> bool:
    """True when this item is a routine PASSED probe result — no diagnostic value.

    Excludes continuous data feeds (_ALWAYS_PIPELINE_PROBES) and out-of-band
    uploads (e.g. discovery/profile data tagged with a non-default
    evidence_source) — those aren't "checked, clean" results, just data.
    """
    return (
        item.result == "PASSED"
        and item.probe not in _ALWAYS_PIPELINE_PROBES
        and item.evidence_source == "RemoteAgent"
    )


async def _store_clean_check(redis: Any, agent_id: str, hostname: str, item: EvidenceItem, ts: str) -> None:
    key = f"{_CHECKS_PREFIX}{agent_id}"
    entry = {
        "ts": item.ts or ts,
        "result": item.result,
        "alert_hint": item.alert_hint[:300],
        "namespace": item.namespace or hostname,
    }
    await redis.hset(key, item.probe, json.dumps(entry))
    await redis.expire(key, _CHECKS_TTL)


async def _check_dedup(redis: Any, agent_id: str, fp: str) -> tuple[int, bool]:
    """
    Track fingerprint occurrence. Returns (count, should_skip_kafka).
    First PASS_COUNT occurrences go to Kafka; after that skip (dedup).
    """
    key = f"{_DEDUP_PREFIX}{agent_id}:{fp}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _DEDUP_WINDOW_S)
    skip_kafka = count > _DEDUP_PASS_COUNT
    return count, skip_kafka


def _resolve_item_domain(item: EvidenceItem) -> str:
    """Lĩnh vực canonical của một mẩu bằng chứng, để gắn vào envelope.

    Thay cho `_classify_item` cũ (gỡ 2026-08-09). Hàm đó tính thêm
    `_quality_tier`/`_quality_score`/`_severity`/`_lm_eligible`/`_archive_eligible`
    và bơm vào envelope, nhưng **không consumer nào đọc** — grep toàn repo (src, ui,
    k8s) chỉ ra đúng một nơi tham chiếu là test của chính nó. Nó cũng chấm điểm theo
    `LANE_SCORE` dựa trên `envelope.lane`, tức trục A đã gỡ khỏi tầng trace.

    Cái DUY NHẤT trong đó có giá trị là `detect_domain` — và trớ trêu là kết quả bị
    đặt vào khoá `_domain` mà không ai đọc, trong khi `evidence_consumer` lại đọc
    `ev_doc["domain"]`. Envelope trước đây KHÔNG có khoá đó, nên bằng chứng từ agent
    tới worker luôn rỗng lĩnh vực (đo tại P1: 0/100% trace có `domain`). Nay trả
    thẳng vào `domain`.

    `domain_hint` do collector tự khai vẫn thắng mọi suy đoán — xem ghi chú cùng nội
    dung ở `remote_agent_pipeline`.
    """
    return detect_domain(
        item.probe, item.alert_hint, item.raw, item.lane,
        domain_hint=getattr(item, "domain", "") or None,
    )


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.post("/register")
async def register_agent(body: AgentRegisterRequest, request: Request) -> JSONResponse:
    """Remote agent gọi endpoint này khi khởi động và mỗi 30s để giữ alive.

    Omni lưu thông tin vào Redis với TTL=120s. Agent không re-register trong 120s
    sẽ bị đánh dấu offline khi list agents.
    """
    redis = _get_redis(request)
    now = int(time.time())

    # A non-admin caller (tenant API key) can only register agents under their
    # own tenant — self-declared body.tenant_id is ignored for them.
    ctx = get_tenant_ctx(request)
    await require_agent_tenant(redis, body.agent_id, ctx,
                               repo=getattr(request.app.state, "admin_repo", None))
    tenant_id = body.tenant_id if is_admin_ctx(ctx) else ctx.tenant_id

    record: dict[str, Any] = {
        "agent_id": body.agent_id,
        "hostname": body.hostname,
        "version": body.version,
        "capabilities": body.capabilities,
        "adapter_domains": sorted({str(domain).strip().lower() for domain in body.adapter_domains if str(domain).strip()}),
        "platform": body.platform,
        "k8s_namespace": body.k8s_namespace,
        "tenant_id": tenant_id,
        "environment_id": getattr(ctx, "environment_id", None),
        "registered_at": now,
        "last_seen": now,
        "type": "remote",
        # Peer identity for connection_scan → connects_to projection
        # (aoip.onboarding_projection.resolve_ip_to_host_map). Prefer the
        # agent's self-reported LAN-facing IP (body.local_ip) over
        # request.client.host: multiple agent hosts behind one NAT egress
        # (e.g. OrbStack's shared gateway) all present the SAME client IP to
        # this endpoint, making host resolution ambiguous by construction —
        # not a spoofing risk here since onboarding/discovery data is already
        # fully self-reported by the agent (process names, ports, etc.), same
        # trust boundary as the rest of this probe family.
        "remote_ip": body.local_ip or (request.client.host if request.client else None),
        "bundle_sha256": body.bundle_sha256,
        "aoip_bundle_sha256": body.aoip_bundle_sha256,
    }

    key = f"{_REGISTRY_PREFIX}{body.agent_id}"
    await redis.set(key, json.dumps(record), ex=_REGISTRY_TTL)

    # Resolve anomaly thresholds from omni_admin runtime flags (write-through
    # cache) so operators can tune them without redeploying agents on customer
    # hosts. Always returns a safe-default bundle on cache miss.
    from services.admin_config.agent_thresholds import resolve_agent_thresholds

    thresholds = await resolve_agent_thresholds(redis, tenant_id)

    logger.info(
        "[AGENT-REGISTER] agent_id=%s hostname=%s caps=%s",
        body.agent_id,
        body.hostname,
        body.capabilities,
    )
    return JSONResponse(content={
        "status": "registered",
        "agent_id": body.agent_id,
        "ttl": _REGISTRY_TTL,
        "server_time": now,
        "config": {"thresholds": thresholds},
    })


@router.post("/evidence")
async def ingest_evidence(body: AgentEvidenceRequest, request: Request) -> JSONResponse:
    """Remote agent POST evidence batch về đây.

    Gateway validate → produce trực tiếp vào omni-diagnostic-evidence (skip prober),
    vì agent đã thu thập OS-level data mà prober không thể reach.
    """
    redis = _get_redis(request)
    kafka = _get_kafka(request)
    topic = _get_evidence_topic(request)
    knowledge_topic = _get_knowledge_topic(request)

    # A non-admin caller (tenant API key) can only push evidence under their
    # own tenant — self-declared body.tenant_id is ignored for them.
    ctx = get_tenant_ctx(request)
    await require_agent_tenant(redis, body.agent_id, ctx,
                               repo=getattr(request.app.state, "admin_repo", None))
    tenant_id = body.tenant_id if is_admin_ctx(ctx) else ctx.tenant_id

    # Circuit breaker check
    cb = await redis.get("omni:circuit_breaker:active")
    if str(cb).strip() == "1":
        raise HTTPException(status_code=503, detail="circuit_breaker_active")

    now_ts = str(int(time.time()))
    enqueued = 0
    hard_blocked = 0
    dedup_skipped = 0
    clean_skipped = 0

    for item in body.evidence:
        # ── Hard block (GIGO) ───────────────────────────────────────────────
        blocked, reason = _is_hard_blocked(item)
        if blocked:
            hard_blocked += 1
            logger.debug("[AGENT-EVIDENCE] hard_block agent=%s probe=%s reason=%s",
                         body.agent_id, item.probe, reason)
            continue

        # ── Clean check (PASSED, no diagnostic value) ────────────────────────
        # Routine "checked, clean" probe results never reach the diagnostic
        # pipeline — there is nothing to diagnose. They land in a per-agent
        # side-channel instead, so a trace/Active-Traces entry is only ever
        # created for something actually worth looking at.
        if _is_clean_check(item):
            clean_skipped += 1
            await _store_clean_check(redis, body.agent_id, body.hostname, item, now_ts)
            continue

        # ── Rate limit ──────────────────────────────────────────────────────
        passes_rl = await _check_rate_limit(redis, body.agent_id, item)
        if not passes_rl:
            logger.warning("[AGENT-EVIDENCE] rate_limit agent=%s probe=%s result=%s",
                           body.agent_id, item.probe, item.result)
            continue

        # ── Dedup / fingerprint ─────────────────────────────────────────────
        fp = fingerprint_evidence({
            "probe": item.probe,
            "alert_hint": item.alert_hint,
            "raw": item.raw,
        })
        dedup_count, skip_kafka = await _check_dedup(redis, body.agent_id, fp)
        if skip_kafka:
            dedup_skipped += 1
            logger.debug("[AGENT-EVIDENCE] dedup_skip agent=%s fp=%s count=%d",
                         body.agent_id, fp, dedup_count)
            # Still update side-channel metrics (EPS, metrics snapshot, logs) — done below
            # but skip Kafka publish
        else:
            # Lĩnh vực canonical — worker đọc `ev_doc["domain"]` ở mark_stage EVIDENCE.
            item_domain = _resolve_item_domain(item)

            envelope: dict[str, Any] = {
                "trace_id": item.trace_id,
                "probe": item.probe,
                "alert_rule": item.alert_rule,
                "alert_hint": sanitize_evidence_field(item.alert_hint),
                "result": item.result,
                "extracted_fact": {
                    **item.extracted_fact,
                    "agent_id": body.agent_id,
                    "hostname": body.hostname,
                },
                "raw": sanitize_evidence_field(item.raw),
                "symptom_group": item.symptom_group,
                "domain": item_domain,
                "lane": item.lane,
                "stream_tags": item.stream_tags or [item.lane],
                "namespace": item.namespace or body.hostname,
                "ts": item.ts or now_ts,
                "evidence_source": item.evidence_source or "RemoteAgent",
                "tenant_id": tenant_id,
                "canonical_query_snippet": json.dumps({
                    "labels": {
                        "agent_id": body.agent_id,
                        "hostname": body.hostname,
                        "probe": item.probe,
                    }
                }),
                "_fingerprint": fp,
                "_dedup_count": dedup_count,
                "signal_type": item.signal_type,
            }

            payload = json.dumps(
                {"data": json.dumps(envelope, ensure_ascii=False)},
                ensure_ascii=False,
            ).encode("utf-8")
            # INV_KNOWLEDGE_NOT_ALERT: route non-ANOMALY signals to knowledge topic.
            dest_topic = topic if item.signal_type == "ANOMALY" else knowledge_topic
            await kafka.send_and_wait(dest_topic, value=payload)
            enqueued += 1

    # Update last_seen in registry
    key = f"{_REGISTRY_PREFIX}{body.agent_id}"
    raw = await redis.get(key)
    if raw:
        try:
            record = json.loads(raw)
            record["last_seen"] = int(time.time())
            record["evidence_count"] = record.get("evidence_count", 0) + enqueued
            await redis.set(key, json.dumps(record), ex=_REGISTRY_TTL)
        except Exception:
            pass

    # ── Side-channel storage for UI (non-blocking best-effort) ──────────────
    now_ms = int(time.time() * 1000)
    eps_key = f"{_EPS_PREFIX}{body.agent_id}"
    try:
        for i, item in enumerate(body.evidence):
            await redis.zadd(eps_key, {f"{now_ms}-{i}": now_ms})
            # Store metrics snapshot for latest system evidence
            if item.probe == "remote_system_metrics" and item.extracted_fact:
                metrics_key = f"{_METRICS_PREFIX}{body.agent_id}"
                snapshot = {**item.extracted_fact, "ts": now_ts}
                await redis.set(metrics_key, json.dumps(snapshot), ex=600)
            # Append log evidence to circular buffer
            if item.probe == "remote_log_errors":
                log_key = f"{_LOGS_PREFIX}{body.agent_id}"
                entry = {
                    "ts": item.ts or now_ts,
                    "probe": item.probe,
                    "result": item.result,
                    "alert_hint": item.alert_hint[:500],
                    "extracted_fact": item.extracted_fact,
                    "raw": item.raw[:500],
                }
                await redis.lpush(log_key, json.dumps(entry))
                await redis.ltrim(log_key, 0, _LOG_MAX - 1)
                await redis.expire(log_key, 3600)
        # Trim EPS window to last 60s
        await redis.zremrangebyscore(eps_key, "-inf", now_ms - _EPS_WINDOW_MS)
        await redis.expire(eps_key, 120)
    except Exception as exc:
        logger.warning("[AGENT-EVIDENCE] side-channel storage failed: %s", exc)

    logger.info(
        "[AGENT-EVIDENCE] agent_id=%s hostname=%s enqueued=%d blocked=%d dedup_skip=%d clean_skip=%d topic=%s",
        body.agent_id,
        body.hostname,
        enqueued,
        hard_blocked,
        dedup_skipped,
        clean_skipped,
        topic,
    )
    return JSONResponse(content={
        "status": "queued",
        "agent_id": body.agent_id,
        "enqueued": enqueued,
        "hard_blocked": hard_blocked,
        "dedup_skipped": dedup_skipped,
        "clean_skipped": clean_skipped,
    })
