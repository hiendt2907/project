"""Per-trace pipeline stage tracker (shared gateway + workers).

Canonical home for PIPELINE_STAGES + mark_stage. Lives under src/pkg/ so BOTH
the gateway image and the worker image can import the same source — no drift,
and the gateway can mark stages (e.g. INGEST) without importing workers/.

Stdlib-only (json/time/logging); the redis client is passed in by the caller.
All writes are best-effort: Redis errors are logged but never propagate.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from pkg.observability.otel_stage_span import emit_stage_span

log = logging.getLogger(__name__)

PIPELINE_STAGES: list[str] = [
    "INGEST",
    "EVIDENCE",
    "RAG",
    "LLM",
    "VERIFY",
    "SCHEMA",
    "KILLSWITCH",
    "CRAT",
    "DISPATCH",
    "HITL",
    "EXECUTOR",
    "FEEDBACK",
    "AUTO_RECOVERY",
]

_VALID_STATUSES = frozenset({"ok", "fail", "skip", "pending"})
_KEY_PREFIX = "omni:trace:stages:"
_EVENTS_STREAM = "omni:trace:events"
_LOGS_KEY_PREFIX = "omni:trace:logs:"
_TTL_SEC = 3600
_STREAM_MAXLEN = 2000
_LOGS_MAXLEN = 400


async def append_trace_log(
    redis: Any,
    trace_id: str,
    phase: str,
    line: str,
    *,
    level: str = "info",
) -> None:
    """Append one raw log line for a trace/phase to omni:trace:logs:{trace}.

    Stored as a capped Redis LIST of JSON ``{ts, phase, level, line}`` (newest last
    via RPUSH + left-trim). Best-effort: swallows Redis errors. Lets the UI render a
    per-phase log stream alongside the pipeline flow.
    """
    if not trace_id or len(trace_id) > 128 or not line:
        return
    key = f"{_LOGS_KEY_PREFIX}{trace_id}"
    entry = json.dumps(
        {"ts": time.time(), "phase": str(phase or "")[:32], "level": str(level or "info")[:12], "line": str(line)[:600]},
        ensure_ascii=False,
    )
    try:
        await redis.rpush(key, entry)
        await redis.ltrim(key, -_LOGS_MAXLEN, -1)
        await redis.expire(key, _TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — logs are best-effort
        log.debug("pipeline_stages: append_trace_log redis error trace=%s err=%s", trace_id, exc)


async def mark_stage(
    redis: Any,
    trace_id: str,
    stage: str,
    status: str = "ok",
    *,
    detail: str = "",
    domain: str = "",
    signal_kind: str = "",
) -> None:
    """Record a pipeline stage transition for trace_id.

    Best-effort: swallows Redis errors with a warning log.
    Validates inputs silently — invalid stage or trace_id is a no-op.

    ``domain`` là một trong 9 domain canonical (`pkg/domain/taxonomy.py`) — trục
    phân loại sự cố duy nhất ở tầng trace.

    ``signal_kind`` = ``diagnostic`` | ``learning``. Trục ĐỘC LẬP với domain: nó trả
    lời "tín hiệu này có phải sự cố không", còn domain trả lời "thuộc lĩnh vực nào".
    Tín hiệu học hỏi (discovery/knowledge, `INV_KNOWLEDGE_NOT_ALERT`) chỉ chạm đúng
    một bước EVIDENCE rồi rẽ; hiển thị chúng như sự cố "đang xử lý" là sai bản chất.

    Cả hai đều **last-non-empty-wins**: giá trị rỗng không bao giờ xoá giá trị đã có.
    Cần vậy vì domain chỉ biết sau `detect_domain()` trong khi INGEST đã mark trước
    đó — nhờ đó chỉ cần MỘT call site sớm nhất mỗi pipeline khai domain.

    Trường ``lane`` đã bị gỡ (2026-08-09). Nó từng gánh BỐN nghĩa khác nhau cùng lúc:
    lane trục A (`SYS_RESOURCE`…), `proof_lane` trục B (`"state"`/`"resource"`/
    `"siem"` — đây mới là chỗ 7 call site truyền `resolve_proof_lane()` vào), loại tín
    hiệu (`ONBOARDING_DISCOVERY`), và chuỗi rỗng. Portal render thẳng nó ở cột "Lĩnh
    vực" nên hiện sai nhãn. `proof_lane` vẫn sống nguyên ở `meta["proof_lane"]` của
    evidence — KHÔNG gộp vào domain; lane trục A vẫn đọc được từ payload agent bản cũ
    qua `lane_to_domain()`.
    """
    if not trace_id or len(trace_id) > 128:
        log.debug("pipeline_stages: skipping invalid trace_id len=%s", len(trace_id or ""))
        return
    if stage not in PIPELINE_STAGES:
        log.debug("pipeline_stages: unknown stage=%s trace=%s", stage, trace_id)
        return
    if status not in _VALID_STATUSES:
        status = "ok"

    ts = time.time()
    key = f"{_KEY_PREFIX}{trace_id}"

    try:
        # Build stage entry (immutable dict copy — never mutate arg)
        stage_entry: dict[str, Any] = {
            "status": status,
            "ts": ts,
            "detail": detail,
        }

        # Read existing meta, keep first started_at
        try:
            raw_meta = await redis.hget(key, "__meta__")
            existing_meta: dict[str, Any] = json.loads(raw_meta) if raw_meta else {}
        except Exception:
            existing_meta = {}

        new_meta: dict[str, Any] = {
            **existing_meta,
            "domain": domain or existing_meta.get("domain", ""),
            "signal_kind": signal_kind or existing_meta.get("signal_kind", ""),
            "trace_id": trace_id,
            "updated_at": ts,
        }
        if "started_at" not in new_meta:
            new_meta = {**new_meta, "started_at": ts}

        await redis.hset(key, stage, json.dumps(stage_entry))
        await redis.hset(key, "__meta__", json.dumps(new_meta))
        await redis.expire(key, _TTL_SEC)

        # Publish to global event stream for SSE consumers
        await redis.xadd(
            _EVENTS_STREAM,
            {
                "trace_id": trace_id,
                "stage": stage,
                "status": status,
                # Consumer SSE đọc thẳng từ stream nên cần cả hai ở đây; giá trị rỗng
                # là hợp lệ (mark trước khi detect_domain chạy) — bên đọc phải lấy
                # meta của trace làm nguồn cuối cùng, không phải một event lẻ.
                "domain": domain,
                "signal_kind": signal_kind,
                "ts": str(ts),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        # Every stage transition is also a per-phase log line (free per-phase log
        # stream at all mark_stage call sites). Level maps status → info/warn.
        _level = "error" if status == "fail" else ("warn" if status == "skip" else "info")
        _logline = f"stage {stage} → {status}" + (f": {detail}" if detail else "")
        await append_trace_log(redis, trace_id, stage, _logline, level=_level)

        # Mirror the stage transition into OpenTelemetry → Tempo, grouped under a
        # trace whose id is derived from trace_id (no-op if OTEL disabled/absent).
        try:
            emit_stage_span(trace_id, stage, status, detail=detail, domain=domain)
        except Exception:  # noqa: BLE001 — telemetry never breaks stage tracking
            pass
    except Exception as exc:
        log.warning(
            "pipeline_stages: redis error stage=%s trace=%s status=%s err=%s",
            stage,
            trace_id,
            status,
            exc,
        )
