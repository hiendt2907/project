"""FileAuditLog — sổ kiểm tamper-evident phía AGENT (CRAT host-side).

Vì sao tồn tại: mọi mutation phục hồi PHẢI để lại bằng chứng không thể sửa lén
(INV_HUMAN_ACCOUNTABILITY, SOX §404). Omni đã có CRAT hash-chain (Redis+Ed25519);
nhưng agent chạy trên host khách, không phải lúc nào cũng có Redis/Kafka tại chỗ —
nên audit được ghi append-only ra ĐĨA phía khách (INV_DATA_RESIDENCY), cùng cơ chế
SHA-256 hash-chain: mỗi block trỏ prev_hash, sửa 1 block là gãy chuỗi.

KHÔNG noun ontology mới: đây là sổ ghi sự kiện vận hành (Action/Decision đã có),
không phải entity tri thức. Event type tái dùng họ CRAT.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

GENESIS_HASH = "0" * 64

# AOIP recovery event types (họ CRAT) — chuỗi ổn định cho audit/replay.
EV_RECOVERY_PLANNED = "RECOVERY_PLANNED"
EV_RECOVERY_GATE_BLOCKED = "RECOVERY_GATE_BLOCKED"
EV_RECOVERY_BEFORE_STATE = "RECOVERY_BEFORE_STATE"
EV_RECOVERY_EXECUTED = "RECOVERY_EXECUTED"          # mutation đã chạy
EV_RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
EV_RECOVERY_VERIFICATION_FAILED = "RECOVERY_VERIFICATION_FAILED"
EV_RECOVERY_ESCALATED = "RECOVERY_ESCALATED"
EV_RECOVERY_RECONCILED = "RECOVERY_RECONCILED"  # idempotent: đã chạy trước, zero mutation mới
EV_RECOVERY_LEASE_DENIED = "RECOVERY_LEASE_DENIED"  # scope bị agent khác giữ


def _payload_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _block_hash(seq: int, event_type: str, trace_id: str, ts: str, p_hash: str, prev: str) -> str:
    raw = f"{seq}|{event_type}|{trace_id}|{ts}|{p_hash}|{prev}".encode()
    return hashlib.sha256(raw).hexdigest()


class FileAuditLog:
    """Append-only JSONL hash-chain, một file một scope (tenant/host)."""

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _blocks(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(line) for line in self._path.read_text().splitlines() if line.strip()]

    def append(self, event_type: str, payload: dict, *, trace_id: str) -> dict:
        blocks = self._blocks()
        prev = blocks[-1]["block_hash"] if blocks else GENESIS_HASH
        seq = len(blocks) + 1
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        p_hash = _payload_hash(payload)
        bh = _block_hash(seq, event_type, trace_id, ts, p_hash, prev)
        block = {
            "seq": seq, "event_type": event_type, "trace_id": trace_id,
            "timestamp_utc": ts, "payload_hash": p_hash, "prev_hash": prev,
            "block_hash": bh, "payload": payload,
        }
        with self._path.open("a") as fh:
            fh.write(json.dumps(block, default=str) + "\n")
        return block

    def verify_chain(self) -> bool:
        """True nếu chuỗi nguyên vẹn (mọi prev_hash + block_hash khớp)."""
        prev = GENESIS_HASH
        for b in self._blocks():
            ph = _payload_hash(b["payload"])
            expect = _block_hash(b["seq"], b["event_type"], b["trace_id"],
                                 b["timestamp_utc"], ph, prev)
            if b["prev_hash"] != prev or b["block_hash"] != expect or b["payload_hash"] != ph:
                return False
            prev = b["block_hash"]
        return True

    def events(self) -> list[str]:
        return [b["event_type"] for b in self._blocks()]
