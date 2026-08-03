"""Knowledge pipeline — xử lý omni-knowledge-evidence (METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY/CHANGE_DETECTED).

INV_KNOWLEDGE_NOT_ALERT — **nới có kiểm soát ngày 2026-07-30** (quyết định của chủ hệ
thống, không phải vi phạm bất biến; xem `plans/lane-to-domain-and-omni-decides-2026-07-30.md`
Phase 6). Định nghĩa hiện hành:

- `METRIC_SAMPLE` **ĐƯỢC phân tích** tại Omni: cập nhật baseline 3σ và phát hiện lệch.
  Phân tích này **thuần số** — không RAG, không LLM.
- Chỉ khi phát hiện lệch, mẫu đó mới được **nâng cấp** thành `signal_type=ANOMALY` và
  đẩy sang `omni-diagnostic-evidence` để đi qua pipeline chẩn đoán đầy đủ (RAG + LLM).
- Vẫn giữ nguyên phần cốt lõi của bất biến: một `METRIC_SAMPLE` **bình thường** không
  gọi LLM, không tạo incident, không rời khỏi pipeline này.
- Các signal khác (`LOG_SAMPLE`/`DISCOVERY`/`CHANGE_DETECTED`/`UNKNOWN_ENTITY`) không
  đổi: thu thập, tích lũy, và hỏi admin khi cần — không tạo incident.

Ai phán là "có lệch" phụ thuộc `ConfidenceLevel` của host (cold-start guard) — xem
`_decide_metric_deviation`. Nguồn phán luôn được ghi vào envelope (`decided_by`) để về
sau chứng minh được Omni đã tự phán, không phải ngưỡng tĩnh trên máy khách.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from anomaly.remote_host_baseline import (
    ConfidenceLevel,
    REMOTE_METRIC_SPECS,
    REMOTE_Z_THRESHOLD,
    metric_domain,
    add_confidence,
    get_confidence_score,
    score_to_level,
    update_remote_host_baseline,
)
from pkg.domain.taxonomy import lane_to_domain
from services.admin_config.agent_thresholds import resolve_agent_thresholds
from workers.handler_context import WorkerHandlerContext

logger = logging.getLogger(__name__)

# Dedup khoá nâng cấp ANOMALY: 1 metric / 1 host / 10 phút. Thiếu khoá này, một host
# CPU cao liên tục bơm 1 ANOMALY mỗi chu kỳ agent (mặc định 60s).
_PROMOTED_PREFIX = "omni:knowledge:promoted:"
_PROMOTED_TTL = 600  # 10 phút

# Sổ đối chiếu z-score ở mức LEARNING: baseline chưa đáng tin để nâng ANOMALY, nhưng
# giữ lại để sau này so với phán quyết của hàng rào tĩnh.
_ZDEV_PREFIX = "omni:knowledge:zdev:"
_ZDEV_MAX = 200
_ZDEV_TTL = 7 * 86400

_DECIDED_BY_BASELINE = "omni_baseline"
_DECIDED_BY_STATIC = "omni_static_guard"

# Sàn biên độ tuyệt đối cho z-score (chống báo giả host phẳng, 2026-07-31). Dưới mức
# này, dù z vượt ngưỡng thống kê cũng KHÔNG nâng ANOMALY — không phải sự cố vận hành.
_METRIC_MIN_ALARM: dict[str, float] = {
    "cpu_percent": 60.0,
    "mem_percent": 70.0,
}
# Metric tăng đơn điệu ⇒ 3σ vô nghĩa; chỉ hàng rào tĩnh quyết định (không qua z-score).
_Z_EXCLUDED_METRICS: frozenset[str] = frozenset({"disk_percent"})

# Rolling log store per agent: omni:knowledge:logs:{agent_id}:rolling
_LOG_STORE_PREFIX = "omni:knowledge:logs:"
_LOG_STORE_SUFFIX = ":rolling"
_LOG_STORE_MAX = 500
_LOG_STORE_TTL = 86400  # 24h

# Pending change approvals: omni:knowledge:change_pending:{tenant}:{change_id}
_CHANGE_PENDING_PREFIX = "omni:knowledge:change_pending:"
_CHANGE_PENDING_TTL = 7 * 86400  # 7d


async def handle_knowledge_evidence(ctx: WorkerHandlerContext, ev_doc: dict[str, Any]) -> None:
    """Dispatcher theo signal_type. Called từ kafka_knowledge_evidence_loop."""
    signal_type = str(ev_doc.get("signal_type") or "UNKNOWN")
    tenant_id = str(ev_doc.get("tenant_id") or "default")
    agent_id = str(ev_doc.get("extracted_fact", {}).get("agent_id") or "unknown")
    hostname = str(ev_doc.get("namespace") or ev_doc.get("extracted_fact", {}).get("hostname") or agent_id)

    if signal_type == "METRIC_SAMPLE":
        await _handle_metric_sample(ctx, ev_doc, tenant_id, hostname, agent_id)
    elif signal_type == "LOG_SAMPLE":
        await _handle_log_sample(ctx, ev_doc, agent_id)
    elif signal_type == "DISCOVERY":
        await _handle_discovery(ctx, ev_doc, tenant_id, agent_id, hostname)
    elif signal_type == "CHANGE_DETECTED":
        await _handle_change_detected(ctx, ev_doc, tenant_id, hostname)
    elif signal_type == "UNKNOWN_ENTITY":
        await _handle_unknown_entity(ctx, ev_doc, tenant_id, hostname)
    else:
        logger.debug("knowledge_pipeline: unknown signal_type=%s agent=%s", signal_type, agent_id)


async def _handle_metric_sample(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
    agent_id: str,
) -> None:
    """METRIC_SAMPLE → update 3σ baseline + add confidence + Omni tự phán có lệch không.

    Phân tích thuần số (không LLM/RAG). Nếu lệch ⇒ nâng thành ANOMALY và đẩy sang
    ``omni-diagnostic-evidence``; nếu bình thường ⇒ dừng ở đây (xem docstring đầu file).
    """
    fact = ev_doc.get("extracted_fact") or {}
    if not isinstance(fact, dict):
        return

    try:
        zscores = await update_remote_host_baseline(
            ctx.redis,
            tenant_id=tenant_id,
            host=hostname,
            fact=fact,
        )
    except Exception as exc:
        logger.warning("knowledge_pipeline: metric_sample baseline err host=%s err=%s", hostname, exc)
        return

    # +1 confidence per 100 samples (tracked via a simple counter)
    counter_key = f"omni:knowledge:metric_count:{tenant_id}:{hostname}"
    try:
        count = await ctx.redis.incr(counter_key)
        await ctx.redis.expire(counter_key, 90 * 86400)  # 90d TTL
        if count % 100 == 0:
            await add_confidence(ctx.redis, tenant_id=tenant_id, host=hostname, delta=1)
            logger.info(
                "knowledge_pipeline: metric_milestone tenant=%s host=%s samples=%d",
                tenant_id, hostname, count,
            )
    except Exception as exc:
        logger.debug("knowledge_pipeline: metric confidence incr err=%s", exc)

    # Đây là chỗ Omni tự phán. Trước 2026-07-30 nhánh này chỉ `logger.debug(zscores)` —
    # tính xong rồi bỏ, nghĩa là người quyết định thật vẫn là ngưỡng tĩnh trên máy khách.
    # Không bọc try/except nuốt lỗi: một lỗi Kafka/Redis thật ở bước nâng cấp phải văng
    # ra tới kafka_knowledge_evidence_loop để đi qua retry+poison-ack sẵn có, thay vì
    # làm một cảnh báo thật biến mất không dấu vết.
    await _decide_and_promote(ctx, ev_doc, tenant_id, hostname, agent_id, fact, zscores)


def _decide_metric_deviation(
    level: ConfidenceLevel,
    fact: dict[str, Any],
    zscores: dict[str, float],
    thresholds: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Phán "có lệch không" theo tầng confidence (cold-start guard).

    | confidence     | ai phán                                                    |
    |----------------|------------------------------------------------------------|
    | STATIC_GUARD   | hàng rào tĩnh (so tại Omni, không phải tại agent)          |
    | LEARNING       | hàng rào tĩnh; z-score chỉ ghi sổ đối chiếu                |
    | ASSISTED       | z-score là chính, hàng rào tĩnh làm cận trên               |
    | AUTONOMOUS     | chỉ z-score                                                |

    Trả `(deviations, z_observations)`: `deviations` là các metric đáng nâng cấp,
    `z_observations` là lệch z-score chỉ để ghi sổ (mức LEARNING).

    Chỉ xét lệch **phía trên**: CPU/mem/disk tụt sâu dưới mức thường không phải sự cố
    tài nguyên, nâng nó thành ANOMALY chỉ tạo cảnh báo rác.

    2026-07-31 (chống báo động giả, giữ mù-đĩa khỏi tái diễn):
    - Sàn biên độ `_METRIC_MIN_ALARM`: z-score chỉ có nghĩa khi giá trị đủ CAO về mặt
      vận hành. Host phẳng (σ nhỏ) làm z bung dù CPU 5% — thống kê "bất thường" nhưng
      vô hại. Sự cố tài nguyên thật phải cao thật.
    - `disk_percent` tăng ĐƠN ĐIỆU ⇒ 3σ vô nghĩa (z phẳng khi đĩa bò 70→99%, bung khi
      40→41%). Đĩa CHỈ do hàng rào tĩnh quyết định, không bao giờ qua z-score.
    - Hàng rào tĩnh là cận trên ở MỌI bậc, kể cả AUTONOMOUS. Trước đây tắt static ở
      AUTONOMOUS khiến đĩa 99% (z-excluded) không báo gì — "càng tin host càng mù".
    """
    deviations: list[dict[str, Any]] = []
    z_observations: list[dict[str, Any]] = []
    use_z = level in (ConfidenceLevel.ASSISTED, ConfidenceLevel.AUTONOMOUS)
    use_static = True  # cận trên tĩnh ở mọi bậc — không bao giờ mù hoàn toàn

    for fact_key, z_key, thr_key in REMOTE_METRIC_SPECS:
        raw = fact.get(fact_key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue

        z = zscores.get(z_key)
        static_thr = thresholds.get(thr_key)
        z_eligible = fact_key not in _Z_EXCLUDED_METRICS
        min_alarm = _METRIC_MIN_ALARM.get(fact_key, 0.0)
        z_breach = (
            z_eligible
            and z is not None
            and z >= REMOTE_Z_THRESHOLD
            and value >= min_alarm
        )
        static_breach = static_thr is not None and value >= static_thr

        record = {
            "metric": fact_key,
            "value": round(value, 3),
            "z_score": z,
            "z_threshold": REMOTE_Z_THRESHOLD,
            "static_threshold": static_thr,
        }

        if use_z and z_breach:
            deviations.append({**record, "decided_by": _DECIDED_BY_BASELINE})
        elif use_static and static_breach:
            deviations.append({**record, "decided_by": _DECIDED_BY_STATIC})
        elif level == ConfidenceLevel.LEARNING and z_breach:
            # Baseline chưa đáng tin (25–49) — ghi sổ, KHÔNG nâng ANOMALY.
            z_observations.append(record)

    return deviations, z_observations


async def _decide_and_promote(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
    agent_id: str,
    fact: dict[str, Any],
    zscores: dict[str, float],
) -> None:
    """Quyết định lệch/không rồi nâng cấp ANOMALY nếu lệch. Thuần số, không LLM
    ở BƯỚC QUYẾT ĐỊNH này — nhưng trước khi nâng một deviation lên toàn bộ vòng
    chẩn đoán (RAG+LLM, tốn nhất), thử phản xạ nhanh: đã có cách sửa đã biết +
    đã kiểm chứng cho đúng host này chưa (xem `_try_known_fix_reflex`)? Đây là
    điểm nối tương đương `proactive_observer.py` cho host không có Prometheus —
    trigger là chính deviation này, không phải PromQL threshold cross."""
    score = await get_confidence_score(ctx.redis, tenant_id=tenant_id, host=hostname)
    level = score_to_level(score)
    thresholds = await resolve_agent_thresholds(ctx.redis, tenant_id)

    deviations, z_observations = _decide_metric_deviation(level, fact, zscores, thresholds)

    for obs in z_observations:
        await _record_z_observation(ctx, tenant_id, hostname, obs, score, level)

    if not deviations:
        logger.debug(
            "knowledge_pipeline: metric_sample normal tenant=%s host=%s level=%s zscores=%s",
            tenant_id, hostname, level.value, zscores,
        )
        return

    for dev in deviations:
        if await _try_known_fix_reflex(ctx, tenant_id, hostname, agent_id, dev):
            continue
        await _promote_to_anomaly(ctx, ev_doc, tenant_id, hostname, fact, zscores, dev, score, level)


async def _try_known_fix_reflex(
    ctx: WorkerHandlerContext,
    tenant_id: str,
    hostname: str,
    agent_id: str,
    dev: dict[str, Any],
) -> bool:
    """True nếu một cách sửa đã biết + đã kiểm chứng vừa được PHÁT LỆNH cho
    deviation này (kết quả thật đến sau, qua `remote_command_outcome_loop`) —
    caller khi đó bỏ qua nâng cấp ANOMALY cho riêng deviation này.

    Đòi hỏi discovery snapshot của agent CÓ THẬT — không snapshot nghĩa là
    chưa biết host này chạy service gì, đi thẳng đường đầy đủ (an toàn hơn là
    liều thực thi trên một host chưa biết gì về nó).
    """
    from execution.memory_normalize import canonical_symptom_text
    from remote_agent.discovery import load_discovery_snapshot
    from workers.remote_known_fix import try_remote_known_fix

    try:
        snapshot = await load_discovery_snapshot(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
    except Exception as exc:
        logger.warning(
            "knowledge_pipeline: discovery snapshot load fail host=%s agent=%s err=%s",
            hostname, agent_id, exc,
        )
        return False
    if not snapshot:
        return False

    known = {str(s.get("name")) for s in snapshot.get("services", []) if s.get("name")}
    if not known:
        return False
    # `systemd.restart_unit` nhận unit bare (`extract_suggested_recovery` tự
    # thêm hậu tố `.service`) nhưng ta không chắc quy ước của MỌI bản ghi cũ
    # trong action_experience — chấp nhận cả hai dạng thay vì đoán một chiều.
    host_scope = frozenset(known) | frozenset(f"{n}.service" for n in known)

    query = canonical_symptom_text(
        f"{dev['metric']} deviation on host {hostname}: value={dev['value']}",
        strip_pods=False,
    )
    import uuid

    trace_id = f"kf-{uuid.uuid4().hex[:12]}"
    threshold = float(getattr(ctx.settings, "action_experience_score_threshold", 0.55))
    result = await try_remote_known_fix(
        ctx,
        query_text=query,
        score_threshold=threshold,
        host_scope=host_scope,
        agent_id=agent_id,
        tenant_id=tenant_id,
        trace_id=trace_id,
    )
    if result.get("resolved"):
        logger.info(
            "knowledge_pipeline: known_fix reflex dispatched tenant=%s host=%s metric=%s "
            "command_id=%s trace=%s",
            tenant_id, hostname, dev["metric"], result.get("command_id"), trace_id,
        )
    return bool(result.get("resolved"))


async def _record_z_observation(
    ctx: WorkerHandlerContext,
    tenant_id: str,
    hostname: str,
    obs: dict[str, Any],
    score: int,
    level: ConfidenceLevel,
) -> None:
    key = f"{_ZDEV_PREFIX}{tenant_id}:{hostname}"
    entry = {**obs, "confidence_score": score, "confidence_level": level.value, "ts": str(int(time.time()))}
    try:
        await ctx.redis.lpush(key, json.dumps(entry, ensure_ascii=False))
        await ctx.redis.ltrim(key, 0, _ZDEV_MAX - 1)
        await ctx.redis.expire(key, _ZDEV_TTL)
    except Exception as exc:
        logger.debug("knowledge_pipeline: zdev store err host=%s err=%s", hostname, exc)


async def _promote_to_anomaly(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
    fact: dict[str, Any],
    zscores: dict[str, float],
    dev: dict[str, Any],
    score: int,
    level: ConfidenceLevel,
) -> None:
    """Nâng METRIC_SAMPLE thành ANOMALY trên omni-diagnostic-evidence (dedup 600s)."""
    kafka = getattr(ctx, "kafka", None)
    if kafka is None:
        logger.info(
            "knowledge_pipeline: metric deviation (no kafka) tenant=%s host=%s metric=%s by=%s",
            tenant_id, hostname, dev["metric"], dev["decided_by"],
        )
        return

    metric = str(dev["metric"])
    dedup_key = f"{_PROMOTED_PREFIX}{tenant_id}:{hostname}:{metric}"
    acquired = await ctx.redis.set(dedup_key, "1", nx=True, ex=_PROMOTED_TTL)
    if not acquired:
        logger.debug(
            "knowledge_pipeline: promotion deduped tenant=%s host=%s metric=%s",
            tenant_id, hostname, metric,
        )
        return

    lane = str(ev_doc.get("lane") or "SYS_RESOURCE")
    # Agent mới khai `domain` trực tiếp; agent cũ thì suy từ lane (có thể ra `unknown`,
    # đó là câu trả lời trung thực — không đoán bừa).
    envelope_domain = str(ev_doc.get("domain") or "") or lane_to_domain(lane)
    # Domain theo METRIC, không theo envelope: `remote_system_metrics` gộp CPU/RAM/đĩa
    # dưới `os_host`, nên đĩa đầy sẽ gọi sai bộ chẩn đoán nếu lấy domain của envelope.
    domain = metric_domain(metric, fallback=envelope_domain)

    decision = {
        "decided_by": dev["decided_by"],
        "decided_at": "omni",
        "metric": metric,
        "value": dev["value"],
        "z_score": dev.get("z_score"),
        "z_threshold": dev.get("z_threshold"),
        "static_threshold": dev.get("static_threshold"),
        "confidence_score": score,
        "confidence_level": level.value,
        "promoted_from": "METRIC_SAMPLE",
    }

    # `result="FAILED"` là BẮT BUỘC, không phải lựa chọn thẩm mỹ:
    # `assess_domain_severity` Priority 1 chỉ nhận đúng chuỗi `"FAILED"` trong
    # `extracted_fact.result` để nâng urgency lên high/critical, và
    # `remote_agent_pipeline` Stage 4 chỉ chạy vòng chẩn đoán nhiều lượt khi
    # `urgency in _NOTIFY_TIERS`. Phát một chuỗi trung thực hơn như `"ANOMALY"` sẽ
    # khiến chính cảnh báo Omni vừa tự phán rơi xuống `medium` — không vòng chẩn
    # đoán, không Telegram, chết lặng ở Stage 4.
    # Ai đã phán và bằng gì thì đọc ở `omni_decision`, không mã hoá vào `result`.
    promoted_fact = {**fact, **zscores, "result": "FAILED", "omni_decision": decision}
    trace = f"{ev_doc.get('trace_id') or f'kp-{uuid.uuid4().hex[:12]}'}-{metric}"

    env = {
        **ev_doc,
        "trace_id": trace,
        "signal_type": "ANOMALY",
        "result": "FAILED",  # xem ghi chú ở promoted_fact — Stage 4 phụ thuộc chuỗi này
        "lane": lane,
        "domain": domain,
        "extracted_fact": promoted_fact,
        "decided_by": dev["decided_by"],
        "omni_decision": decision,
        "alert_rule": f"OmniPromoted_{metric}",
        "alert_hint": (
            f"Omni phát hiện {metric}={dev['value']} lệch trên host {hostname} "
            f"(z={dev.get('z_score')}, ngưỡng tĩnh={dev.get('static_threshold')}, "
            f"confidence={score}/{level.value}, nguồn phán={dev['decided_by']})"
        ),
        "ts": str(int(time.time())),
    }

    topic = getattr(ctx.settings, "kafka_topic_diagnostic_evidence", "omni-diagnostic-evidence")
    try:
        await kafka.send_dict(
            topic,
            {"data": json.dumps(env, ensure_ascii=False)},
            key=trace.encode("utf-8", errors="ignore"),
        )
    except Exception:
        # Nhả khoá dedup rồi để lỗi văng ra cho retry+poison-ack: giữ khoá lại sẽ làm
        # mất hẳn cảnh báo này trong 10 phút tới.
        try:
            await ctx.redis.delete(dedup_key)
        except Exception:
            pass
        raise

    logger.info(
        "knowledge_pipeline: promoted ANOMALY tenant=%s host=%s metric=%s by=%s "
        "z=%s value=%s confidence=%d/%s domain=%s topic=%s",
        tenant_id, hostname, metric, dev["decided_by"], dev.get("z_score"),
        dev["value"], score, level.value, domain, topic,
    )


async def _handle_log_sample(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    agent_id: str,
) -> None:
    """LOG_SAMPLE → rolling log store (RAG context for future queries)."""
    key = f"{_LOG_STORE_PREFIX}{agent_id}{_LOG_STORE_SUFFIX}"
    entry = {
        "ts": ev_doc.get("ts") or str(int(time.time())),
        "alert_hint": (ev_doc.get("alert_hint") or "")[:500],
        "extracted_fact": ev_doc.get("extracted_fact") or {},
        "raw": (ev_doc.get("raw") or "")[:500],
    }
    try:
        await ctx.redis.lpush(key, json.dumps(entry, ensure_ascii=False))
        await ctx.redis.ltrim(key, 0, _LOG_STORE_MAX - 1)
        await ctx.redis.expire(key, _LOG_STORE_TTL)
    except Exception as exc:
        logger.debug("knowledge_pipeline: log_sample store err agent=%s err=%s", agent_id, exc)


async def _handle_discovery(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    agent_id: str,
    hostname: str,
) -> None:
    """DISCOVERY → diff với baseline → emit CHANGE_DETECTED nếu có thay đổi."""
    from remote_agent.discovery import (
        save_discovery_snapshot,
        load_discovery_snapshot,
        diff_discovery,
        is_snapshot_suspect,
        bump_suspect_streak,
        reset_suspect_streak,
        suspect_confirm_threshold,
    )

    probe = str(ev_doc.get("probe") or "unknown")
    fact = ev_doc.get("extracted_fact") or {}
    discovery_data = fact.get("discovery_data") if isinstance(fact, dict) else None
    if not isinstance(discovery_data, dict):
        return

    # Services snapshot (từ service_topology probe) — compare và detect changes.
    # KHÔNG bọc try/except nuốt lỗi ở đây: một lỗi Redis đọc/ghi thật (không
    # phải "key chưa tồn tại", xem load_discovery_snapshot) phải văng ra tới
    # caller (kafka_knowledge_evidence_loop) để đi qua retry+poison-ack sẵn
    # có, thay vì bị nuốt âm thầm khiến chu kỳ diff đó biến mất không dấu vết.
    if probe == "service_topology":
        new_snapshot = discovery_data
        old_snapshot = await load_discovery_snapshot(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
        suspect = old_snapshot is not None and is_snapshot_suspect(old_snapshot, new_snapshot)
        if suspect:
            threshold = suspect_confirm_threshold()
            streak = await bump_suspect_streak(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
            if streak < threshold:
                # 1 chu kỳ rỗng bất thường có thể là collector blip thoáng qua
                # (systemctl timeout/dbus hiccup) — KHÔNG diff, KHÔNG ghi đè
                # baseline, tránh làm hỏng vĩnh viễn system model từ 1 lần lỗi.
                logger.warning(
                    "knowledge_pipeline: discovery snapshot suspect (services=0, "
                    "prev=%d, streak=%d/%d) tenant=%s host=%s — skip diff+baseline overwrite",
                    len(old_snapshot.get("services", [])), streak, threshold,
                    tenant_id, hostname,
                )
                return
            # Xác nhận qua >=2 chu kỳ liên tiếp — chấp nhận là thật (vd. toàn bộ
            # service trên host thật sự down), không còn coi là collector blip.
            await reset_suspect_streak(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
        elif old_snapshot is not None:
            await reset_suspect_streak(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)

        if old_snapshot is not None:
            changes = diff_discovery(old_snapshot, new_snapshot)
            for change in changes:
                await _emit_change_detected(ctx, tenant_id, hostname, change, agent_id)
        # Lưu snapshot mới (baseline cập nhật). Đặt SAU khi diff+emit đã xong
        # để 1 retry (do bước forward bên dưới lỗi) load lại baseline == snapshot
        # mới, diff ra rỗng — không phát trùng change-detected/Telegram.
        await save_discovery_snapshot(ctx.redis, tenant_id=tenant_id, agent_id=agent_id, snapshot=new_snapshot)

    # Forward to omni-discovery-evidence so onboarding worker accumulates facts.
    # Không nuốt lỗi: 1 lần Kafka chập chờn đúng lúc forward trước đây làm
    # evidence biến mất vĩnh viễn (offset nguồn đã commit ngay sau khi hàm
    # này return không lỗi) — để lỗi văng ra cho retry+poison-ack xử lý.
    kafka = getattr(ctx, "kafka", None)
    if kafka is not None:
        discovery_topic = getattr(
            ctx.settings, "kafka_topic_discovery_evidence", "omni-discovery-evidence"
        )
        trace = str(ev_doc.get("trace_id") or agent_id)
        await kafka.send_dict(
            discovery_topic,
            {"data": json.dumps(ev_doc, ensure_ascii=False)},
            key=trace.encode("utf-8", errors="ignore"),
        )
        logger.info(
            "knowledge_pipeline: discovery forwarded tenant=%s probe=%s topic=%s",
            tenant_id, probe, discovery_topic,
        )

    logger.debug("knowledge_pipeline: discovery probe=%s agent=%s", probe, agent_id)


async def _emit_change_detected(
    ctx: WorkerHandlerContext,
    tenant_id: str,
    hostname: str,
    change: dict[str, Any],
    agent_id: str,
) -> None:
    """Lưu change pending + gửi Telegram inline keyboard approve/reject."""
    change_id = uuid.uuid4().hex[:12]
    pending_key = f"{_CHANGE_PENDING_PREFIX}{tenant_id}:{change_id}"
    change_type = change.get("change_type", "UNKNOWN")
    entity_name = change.get("entity_name", "unknown")
    entity_type = change.get("entity_type", "service")

    pending = {
        "change_id": change_id,
        "tenant_id": tenant_id,
        "hostname": hostname,
        "agent_id": agent_id,
        "change_type": change_type,
        "entity_type": entity_type,
        "entity_name": entity_name,
        "old_value": change.get("old_value", ""),
        "new_value": change.get("new_value", ""),
        "ts": str(int(time.time())),
        "status": "pending",
    }
    try:
        await ctx.redis.set(pending_key, json.dumps(pending), ex=_CHANGE_PENDING_TTL)
    except Exception as exc:
        logger.warning("knowledge_pipeline: change_pending store err change_id=%s err=%s", change_id, exc)

    if ctx.telegram is None or not ctx.telegram_chat_id:
        logger.info(
            "knowledge_pipeline: change_detected (no telegram) tenant=%s host=%s type=%s entity=%s",
            tenant_id, hostname, change_type, entity_name,
        )
        return

    _CHANGE_ICONS = {
        "SERVICE_ADDED": ("🔍", "Service mới xuất hiện"),
        "SERVICE_REMOVED": ("⚠️", "Service biến mất"),
        "PORT_OPENED": ("🔓", "Port mới mở"),
        "PORT_CLOSED": ("🔒", "Port đóng"),
    }
    icon, label = _CHANGE_ICONS.get(change_type, ("❓", change_type))
    text = (
        f"{icon} <b>{label}</b> trên <code>{hostname}</code>\n"
        f"Thực thể: <code>{entity_type}</code> / <code>{entity_name}</code>\n"
        f"Trước: {change.get('old_value') or '(trống)'} → Sau: {change.get('new_value') or '(trống)'}\n\n"
        f"Approve để cập nhật baseline. Reject nếu không mong đợi."
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"change_approve:{change_id}"},
            {"text": "❌ Reject", "callback_data": f"change_reject:{change_id}"},
        ]]
    }

    try:
        await ctx.telegram.send_message(
            chat_id=ctx.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        logger.info(
            "knowledge_pipeline: change_telegram_sent change_id=%s host=%s type=%s entity=%s",
            change_id, hostname, change_type, entity_name,
        )
    except Exception as exc:
        logger.warning("knowledge_pipeline: telegram send err change_id=%s err=%s", change_id, exc)


async def _handle_change_detected(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
) -> None:
    """CHANGE_DETECTED envelope (từ agent emit trực tiếp) — delegate đến change pending flow."""
    fact = ev_doc.get("extracted_fact") or {}
    if not isinstance(fact, dict):
        return
    agent_id = str(fact.get("agent_id") or "unknown")
    change = {
        "change_type": str(fact.get("change_type") or "UNKNOWN"),
        "entity_type": str(fact.get("entity_type") or "service"),
        "entity_name": str(fact.get("entity_name") or fact.get("service_name") or "unknown"),
        "old_value": str(fact.get("old_value") or ""),
        "new_value": str(fact.get("new_value") or ""),
    }
    await _emit_change_detected(ctx, tenant_id, hostname, change, agent_id)


async def _handle_unknown_entity(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
) -> None:
    """UNKNOWN_ENTITY → Telegram hỏi admin, lưu pending question."""
    fact = ev_doc.get("extracted_fact") or {}
    if not isinstance(fact, dict):
        return

    entity_type = str(fact.get("entity_type") or "process")
    entity_name = str(fact.get("entity_name") or "unknown")
    port = fact.get("port")

    if ctx.telegram is None or not ctx.telegram_chat_id:
        logger.info(
            "knowledge_pipeline: unknown_entity (no telegram) host=%s entity=%s:%s",
            hostname, entity_type, entity_name,
        )
        return

    msg_id_key = f"omni:knowledge:pending_q:{tenant_id}:{uuid.uuid4().hex[:12]}"
    question = (
        f"❓ <b>Thực thể không rõ</b> trên <code>{hostname}</code>\n"
        f"Loại: <code>{entity_type}</code>  Tên: <code>{entity_name}</code>"
        + (f"  Port: <code>{port}</code>" if port else "")
        + "\n\nBạn mô tả service này là gì? Gửi tài liệu (PDF/ảnh) hoặc text trả lời tin nhắn này."
    )
    try:
        sent = await ctx.telegram.send_message(
            chat_id=ctx.telegram_chat_id,
            text=question,
            parse_mode="HTML",
        )
        msg_id = sent.get("result", {}).get("message_id") if isinstance(sent, dict) else None
        if msg_id:
            pending = {
                "question": question,
                "hostname": hostname,
                "entity_type": entity_type,
                "entity_name": entity_name,
                "ts": str(int(time.time())),
            }
            await ctx.redis.set(msg_id_key, json.dumps(pending), ex=7 * 86400)
            # Also index by Telegram message_id for reply-detection in handlers.py
            await ctx.redis.set(
                f"omni:knowledge:pending_q_by_msgid:{ctx.telegram_chat_id}:{msg_id}",
                msg_id_key,
                ex=7 * 86400,
            )
    except Exception as exc:
        logger.warning("knowledge_pipeline: unknown_entity telegram err host=%s err=%s", hostname, exc)


async def handle_telegram_doc_upload(ctx: WorkerHandlerContext, u: dict[str, Any]) -> bool:
    """Phát hiện admin reply bằng tài liệu (document/photo) → ingest vào knowledge store.

    Trả True nếu đã xử lý (caller skip), False nếu không phải doc upload.
    """
    msg = u.get("message") or u.get("edited_message")
    if not isinstance(msg, dict):
        return False

    doc = msg.get("document")
    photo_list = msg.get("photo")
    if doc is None and not photo_list:
        return False

    # Chỉ xử lý nếu là reply cho một pending_q
    reply_to = msg.get("reply_to_message") or {}
    reply_msg_id = reply_to.get("message_id")
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")

    if not reply_msg_id or not chat_id:
        return False

    q_ref_key = f"omni:knowledge:pending_q_by_msgid:{chat_id}:{reply_msg_id}"
    try:
        q_key = await ctx.redis.get(q_ref_key)
    except Exception:
        return False
    if not q_key:
        return False  # Reply cho tin nhắn khác — bỏ qua

    # Xác định file_id và file_name
    if doc:
        file_id = doc.get("file_id", "")
        file_name = doc.get("file_name") or "document"
        mime = doc.get("mime_type") or ""
    else:
        # photo: lấy ảnh lớn nhất
        largest = max(photo_list, key=lambda p: p.get("file_size", 0))
        file_id = largest.get("file_id", "")
        file_name = "photo.jpg"
        mime = "image/jpeg"

    caption = (msg.get("caption") or "").strip()[:2000] or f"[{mime or 'file'}] {file_name}"

    from services.knowledge.document_store import ingest_customer_knowledge
    from anomaly.remote_host_baseline import add_confidence

    try:
        # Lấy context từ pending question
        q_raw = await ctx.redis.get(q_key)
        q_data = json.loads(q_raw) if q_raw else {}
        tenant_id = str(q_data.get("tenant_id") or "default")
        agent_id = str(q_data.get("agent_id") or "unknown")
        hostname = str(q_data.get("hostname") or agent_id)

        doc_id = await ingest_customer_knowledge(
            ctx.redis,
            tenant_id=tenant_id,
            agent_id=agent_id,
            file_id=file_id,
            file_name=file_name,
            summary=caption,
            uploaded_by="telegram_admin",
        )

        # +20 confidence cho việc upload tài liệu (dữ liệu có giá trị cao)
        await add_confidence(ctx.redis, tenant_id=tenant_id, host=hostname, delta=20)

        # Xoá pending question sau khi xử lý
        await ctx.redis.delete(q_key, q_ref_key)

        if ctx.telegram and chat_id:
            await ctx.telegram.send_message(
                chat_id=chat_id,
                text=f"✅ Đã lưu tài liệu <code>{file_name}</code> cho host <code>{hostname}</code> (doc_id: {doc_id[:16]}…)",
                parse_mode="HTML",
            )
        logger.info(
            "knowledge_pipeline: doc_uploaded doc_id=%s tenant=%s host=%s",
            doc_id, tenant_id, hostname,
        )
    except Exception as exc:
        logger.warning("knowledge_pipeline: doc_upload err file=%s err=%r", file_name, exc)

    return True
