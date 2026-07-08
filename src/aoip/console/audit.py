"""Provider Audit — read-only CRAT hash-chain projection.

Source of truth is `services.audit_ledger.chain_writer` (SHA-256 hash-chain +
Ed25519 signing, tamper-evident). This module only reads the tenant-scoped
Redis lists it already writes to; it never writes.
"""
from __future__ import annotations

import json
from typing import Any

_DEFAULT_BLOCKS_KEY = "audit_chain:blocks"
_NAMED_BLOCKS_PATTERN = "audit_chain:*:blocks"

_LIMIT = 200


def _loads(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        block = json.loads(raw)
        return block if isinstance(block, dict) else None
    except Exception:
        return None


def _tenant_from_key(key: str) -> str:
    # "audit_chain:{tenant_id}:blocks" -> tenant_id
    parts = key.split(":")
    return parts[1] if len(parts) == 3 else "default"


async def build_provider_audit(redis: Any, *, tenant_id: str | None = None) -> dict[str, Any]:
    keys = [_DEFAULT_BLOCKS_KEY, *sorted(await redis.keys(_NAMED_BLOCKS_PATTERN))]
    blocks: list[dict[str, Any]] = []

    for key in keys:
        key_tenant = _tenant_from_key(key)
        if tenant_id is not None and key_tenant != tenant_id:
            continue
        raw_items = await redis.lrange(key, -_LIMIT, -1)
        for raw in raw_items:
            block = _loads(raw)
            if block is None:
                continue
            blocks.append({
                "seq": int(block.get("seq") or 0),
                "event_type": str(block.get("event_type") or ""),
                "trace_id": str(block.get("trace_id") or ""),
                "tenant_id": str(block.get("tenant_id") or key_tenant),
                "timestamp_utc": str(block.get("timestamp_utc") or ""),
                "block_hash": str(block.get("block_hash") or ""),
                "signed": bool(block.get("signature_hex")),
            })

    blocks.sort(key=lambda b: b["timestamp_utc"], reverse=True)
    blocks = blocks[:_LIMIT]

    event_counts: dict[str, int] = {}
    for b in blocks:
        event_counts[b["event_type"]] = event_counts.get(b["event_type"], 0) + 1

    return {
        "total": len(blocks),
        "signed": sum(1 for b in blocks if b["signed"]),
        "event_counts": event_counts,
        "blocks": blocks,
    }
