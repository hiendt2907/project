"""Tamper-evident hash-chain ledger — SHA-256 chaining + Ed25519 signing.

Block N's hash includes Block N-1's hash, making retrospective tampering detectable.
All writes are fail-closed: AuditLedgerError is raised on ANY failure so callers abort.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import time
from typing import Any

from services.audit_ledger.signer import AuditLedgerError, public_key_hex, public_key_version, sign_block_hash

logger = logging.getLogger(__name__)

try:
    from workers.metrics_exporter import observe_crat_write_seconds as _observe_crat_write_seconds
except ImportError:

    def _observe_crat_write_seconds(_seconds: float) -> None:  # type: ignore[misc]
        pass

_REDIS_HEAD_KEY = "audit_chain:head_hash"
_REDIS_SEQ_KEY = "audit_chain:seq"
_REDIS_BLOCKS_KEY = "audit_chain:blocks"
# Public aliases for E2E / ops scripts (avoid duplicating Redis key literals).
REDIS_AUDIT_HEAD_KEY = _REDIS_HEAD_KEY
REDIS_AUDIT_SEQ_KEY = _REDIS_SEQ_KEY
REDIS_AUDIT_BLOCKS_KEY = _REDIS_BLOCKS_KEY
_GENESIS_HASH = "0" * 64

# Serialize block writes so seq + prev_hash are always consistent.
_LOCK: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _compute_block_hash(
    seq: int,
    event_type: str,
    trace_id: str,
    timestamp_utc: str,
    p_hash: str,
    prev_hash: str,
) -> str:
    canonical = f"{seq}|{event_type}|{trace_id}|{timestamp_utc}|{p_hash}|{prev_hash}"
    return hashlib.sha256(canonical.encode()).hexdigest()


async def write_audit_block(
    *,
    event_type: str,
    trace_id: str,
    payload: dict[str, Any],
    redis: Any,
    kafka: Any,
    kafka_topic: str,
) -> dict[str, Any]:
    """Write one tamper-evident block to Redis chain + Kafka topic.

    Raises AuditLedgerError on any failure — callers MUST abort the transaction.
    """
    async with _get_lock():
        try:
            _t0 = time.monotonic()
            # 1. Fetch prev_hash and increment seq atomically
            pipe = redis.pipeline()
            pipe.get(_REDIS_HEAD_KEY)
            pipe.incr(_REDIS_SEQ_KEY)
            results = await pipe.execute()
            prev_hash: str = results[0] or _GENESIS_HASH
            seq: int = int(results[1])

            # 2. Compute block hash
            timestamp_utc = datetime.datetime.now(datetime.UTC).isoformat()
            p_hash = _payload_hash(payload)
            block_hash = _compute_block_hash(seq, event_type, trace_id, timestamp_utc, p_hash, prev_hash)

            # 3. Sign (optional — None when OMNI_AUDIT_PRIVATE_KEY_PATH not set)
            sig_hex = sign_block_hash(block_hash)
            pub_hex = public_key_hex()

            block: dict[str, Any] = {
                "seq": seq,
                "event_type": event_type,
                "trace_id": trace_id,
                "timestamp_utc": timestamp_utc,
                "payload_hash": p_hash,
                "prev_hash": prev_hash,
                "block_hash": block_hash,
                "signature_hex": sig_hex,
                "public_key_hex": pub_hex,
                "pub_key_version": public_key_version(),
                "payload": payload,
            }
            block_json = json.dumps(block, default=str)

            # 4. Atomically persist chain head + append block
            pipe2 = redis.pipeline()
            pipe2.set(_REDIS_HEAD_KEY, block_hash)
            pipe2.rpush(_REDIS_BLOCKS_KEY, block_json)
            await pipe2.execute()

            # 5. Publish to Kafka — round-trip through block_json for JSON-safety;
            #    pass seq as key because omni-audit-chain is a compacted topic.
            if kafka is not None:
                await kafka.send_dict(
                    kafka_topic,
                    json.loads(block_json),
                    key=str(seq).encode(),
                )
            else:
                logger.warning(
                    "event=audit_kafka_unavailable seq=%d trace=%s — Kafka not configured",
                    seq,
                    trace_id,
                )

            _elapsed = time.monotonic() - _t0
            _crat_write_ms = int(_elapsed * 1000)
            _observe_crat_write_seconds(_elapsed)
            logger.info(
                "event=audit_block_written seq=%d event_type=%s trace=%s signed=%s crat_write_ms=%d",
                seq,
                event_type,
                trace_id,
                sig_hex is not None,
                _crat_write_ms,
            )
            return block

        except AuditLedgerError:
            raise
        except Exception as exc:
            raise AuditLedgerError(f"audit_chain write failed: {exc}") from exc
