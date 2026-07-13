"""Command outcome durability — PG ledger cho ``omni:cmd:rec:*`` (IT-6, ADR-002).

Redis vẫn là runtime authority của delivery state machine (``agent_runtime.py``);
PG là bản ghi BỀN VỮNG sống lâu hơn TTL Redis / Redis flush / gateway restart.

Ba đường ghi, tất cả hội tụ về bất biến "đúng MỘT terminal outcome mỗi command":
1. ``pg_record_enqueue`` — hot path enqueue, INSERT ON CONFLICT DO NOTHING.
2. ``pg_record_terminal`` — hot path terminal report; UPSERT nhưng chỉ UPDATE khi
   ``terminal_at IS NULL`` (first-writer-wins, duplicate/conflict không ghi đè).
3. ``reconcile_commands_from_redis`` — safety net chạy lúc gateway startup: SCAN
   record Redis, backfill mọi terminal record PG chưa có/chưa terminal. Bù cho
   khoảng PG down đúng lúc agent report (hot path best-effort, không chặn ACK).

Caller (gateway) gọi hot path best-effort: PG lỗi → log + tiếp tục, KHÔNG fail
request về agent — outcome đã durable ở Redis, reconciler sẽ backfill. Fail request
ở đây sẽ khiến agent retry vô hạn dù mutation đã xong (tệ hơn thiếu 1 dòng PG tạm thời).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from aoip.protocol import TERMINAL_STATES

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO omni_admin.agent_command_outcome
    (tenant_id, command_id, agent_id, mission_id, incident_id, decision_id,
     action_id, canonical_scope, payload_hash, state, outcome, delivery_attempt,
     created_at, terminal_at, recorded_at, source)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14,now(),$15)
ON CONFLICT (tenant_id, command_id) DO NOTHING
"""

# First-writer-wins: chỉ ghi terminal khi hàng hiện tại CHƯA terminal.
_TERMINAL_UPSERT_SQL = """
INSERT INTO omni_admin.agent_command_outcome
    (tenant_id, command_id, agent_id, mission_id, incident_id, decision_id,
     action_id, canonical_scope, payload_hash, state, outcome, delivery_attempt,
     created_at, terminal_at, recorded_at, source)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14,now(),$15)
ON CONFLICT (tenant_id, command_id) DO UPDATE SET
    state = EXCLUDED.state,
    outcome = EXCLUDED.outcome,
    delivery_attempt = EXCLUDED.delivery_attempt,
    terminal_at = EXCLUDED.terminal_at,
    recorded_at = now(),
    source = EXCLUDED.source
WHERE omni_admin.agent_command_outcome.terminal_at IS NULL
RETURNING state
"""


def _ts(epoch: Any) -> datetime | None:
    try:
        e = int(epoch or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(e, tz=timezone.utc) if e > 0 else None


def _row_args(rec: dict, *, source: str) -> list[Any]:
    return [
        rec.get("tenant_id", ""), rec.get("command_id", ""), rec.get("agent_id", ""),
        rec.get("mission_id", ""), rec.get("incident_id", ""), rec.get("decision_id", ""),
        rec.get("action_id", ""), rec.get("canonical_scope", ""), rec.get("payload_hash", ""),
        rec.get("state", ""), json.dumps(rec.get("outcome") or {}),
        int(rec.get("delivery_attempt") or 0),
        _ts(rec.get("created_at")) or datetime.now(tz=timezone.utc),
        _ts(rec.get("terminal_at")), source,
    ]


async def pg_record_enqueue(pool: Any, rec: dict, *, source: str = "gateway") -> bool:
    """Ghi hàng QUEUED lúc enqueue. Idempotent (conflict → no-op). True nếu ghi được."""
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INSERT_SQL, *_row_args(rec, source=source))
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, reconciler backfill
        logger.warning("cmd_ledger.enqueue_pg_fail command_id=%s err=%s",
                       rec.get("command_id"), exc)
        return False


async def pg_record_terminal(pool: Any, rec: dict, *, source: str = "gateway") -> str:
    """Ghi terminal outcome. Trả 'recorded' | 'already_terminal' | 'skipped' | 'error'.

    'already_terminal' = hàng PG đã có terminal outcome trước đó — bất biến
    đúng-một-outcome giữ nguyên, lần ghi này bị bỏ (first-writer-wins).
    """
    if pool is None:
        return "skipped"
    if rec.get("state") not in TERMINAL_STATES:
        return "skipped"
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_TERMINAL_UPSERT_SQL, *_row_args(rec, source=source))
        return "recorded" if row is not None else "already_terminal"
    except Exception as exc:  # noqa: BLE001 — best-effort, reconciler backfill
        logger.warning("cmd_ledger.terminal_pg_fail command_id=%s err=%s",
                       rec.get("command_id"), exc)
        return "error"


async def reconcile_commands_from_redis(pool: Any, redis: Any) -> dict[str, int]:
    """Backfill PG từ Redis (safety net, chạy lúc gateway startup).

    Quét ``omni:cmd:rec:*``; record terminal → ``pg_record_terminal`` (source=
    reconcile), record chưa terminal → đảm bảo có hàng QUEUED/progress. Trả đếm
    theo kết quả để log/quan sát.
    """
    counts = {"scanned": 0, "recorded": 0, "already_terminal": 0, "inserted_open": 0, "error": 0}
    if pool is None or redis is None:
        return counts
    async for key in redis.scan_iter(match="omni:cmd:rec:*", count=200):
        raw = await redis.get(key)
        if raw is None:
            continue
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError):
            continue
        counts["scanned"] += 1
        if rec.get("state") in TERMINAL_STATES:
            result = await pg_record_terminal(pool, rec, source="reconcile")
            counts["recorded" if result == "recorded" else
                   "already_terminal" if result == "already_terminal" else "error"] += 1
        else:
            if await pg_record_enqueue(pool, rec, source="reconcile"):
                counts["inserted_open"] += 1
    logger.info("cmd_ledger.reconcile done %s", counts)
    return counts
