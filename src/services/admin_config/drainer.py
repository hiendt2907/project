"""CRAT outbox drainer — Transactional Outbox tail (MASTER_PLAN §7).

Đọc omni_admin.crat_outbox WHERE status='PENDING' (FOR UPDATE SKIP LOCKED), ghi
write_audit_block (fail-closed), cập nhật status=SENT + crat_ref. At-least-once;
idempotent qua dedup_key UNIQUE (CRAT chain không nhân đôi). Kill drainer giữa
chừng → restart đọc lại PENDING, không mất block.

Chạy ở role analyst/full (loop riêng). Metric omni_crat_outbox_pending để alert
khi CRAT chain tụt hậu so với config.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block

logger = logging.getLogger(__name__)

# Kafka topic CRAT chain (compacted, key=seq) — khớp chain_writer caller hiện hữu.
_CRAT_KAFKA_TOPIC = "omni-audit-chain"


class CratOutboxDrainer:
    """Background drain loop. Dừng bằng ``stop()`` / cancel task."""

    def __init__(
        self,
        pool: Any,
        *,
        redis: Any,
        kafka: Any,
        settings: Any,
        kafka_topic: str = _CRAT_KAFKA_TOPIC,
    ) -> None:
        self._pool = pool
        self._redis = redis
        self._kafka = kafka
        self._topic = kafka_topic
        self._poll = float(getattr(settings, "crat_outbox_poll_interval_sec", 5.0))
        self._batch = int(getattr(settings, "crat_outbox_batch_size", 32))
        self._max_attempts = int(getattr(settings, "crat_outbox_max_attempts", 10))
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def pending_count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS n FROM omni_admin.crat_outbox WHERE status = 'PENDING'"
            )
        return int(row["n"]) if row else 0

    async def drain_once(self) -> int:
        """Drain tối đa 1 batch. Trả số block ghi thành công."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT id, dedup_key, event_type, payload, attempts "
                    "FROM omni_admin.crat_outbox WHERE status = 'PENDING' "
                    "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT $1",
                    self._batch,
                )
                sent = 0
                for row in rows:
                    ok = await self._write_block(conn, row)
                    if ok:
                        sent += 1
        return sent

    async def _write_block(self, conn: Any, row: Any) -> bool:
        import json

        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        dedup_key = row["dedup_key"]
        try:
            block = await write_audit_block(
                event_type=row["event_type"],
                trace_id=f"cfg:{dedup_key}",
                payload=payload,
                redis=self._redis,
                kafka=self._kafka,
                kafka_topic=self._topic,
                tenant_id=str(payload.get("tenant_id", "default")),
            )
            await conn.execute(
                "UPDATE omni_admin.crat_outbox SET status='SENT', crat_ref=$2, "
                "sent_at=now(), attempts=attempts+1 WHERE id=$1",
                row["id"],
                block.get("block_hash"),
            )
            return True
        except Exception as exc:  # noqa: BLE001 — fail-closed: giữ PENDING, retry
            attempts = int(row["attempts"]) + 1
            status = "FAILED" if attempts >= self._max_attempts else "PENDING"
            await conn.execute(
                "UPDATE omni_admin.crat_outbox SET status=$2, attempts=$3, last_error=$4 "
                "WHERE id=$1",
                row["id"],
                status,
                attempts,
                str(exc)[:500],
            )
            logger.warning(
                "crat_outbox: write_audit_block fail dedup=%s attempt=%d status=%s err=%s",
                dedup_key,
                attempts,
                status,
                exc,
            )
            return False

    async def run(self) -> None:
        """Loop tới khi ``stop()``. Lỗi 1 vòng không giết loop (log + tiếp)."""
        logger.info("crat_outbox drainer started poll=%.1fs batch=%d", self._poll, self._batch)
        while not self._stopped.is_set():
            try:
                sent = await self.drain_once()
                if sent:
                    logger.info("crat_outbox: drained %d block(s)", sent)
            except Exception as exc:  # noqa: BLE001
                logger.error("crat_outbox drain loop error: %s", exc)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass
        logger.info("crat_outbox drainer stopped")
