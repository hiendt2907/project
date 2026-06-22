"""Unit chaos tests — CRAT chain verifier and signer.

Tests verify_block_hash, verify_chain integrity, and signer edge cases
(unsigned mode when OMNI_AUDIT_PRIVATE_KEY_PATH is not set).
"""

from __future__ import annotations

import hashlib
import json
import os

import fakeredis.aioredis
import pytest

from services.audit_ledger.verifier import verify_block_hash, verify_chain, VerifyResult
from services.audit_ledger.signer import AuditLedgerError, public_key_version, sign_block_hash, public_key_hex


# ── Verifier tests ─────────────────────────────────────────────────────────────


def _make_block(
    seq: int = 1,
    event_type: str = "ADVISORY_DECISION",
    trace_id: str = "t001",
    prev_hash: str = "0" * 64,
    payload: dict | None = None,
) -> dict:
    """Build a valid CRAT block with correct hash."""
    from services.audit_ledger.chain_writer import _compute_block_hash, _payload_hash
    import datetime

    p = payload or {"verdict": "SUGGEST_REMEDIATION"}
    timestamp_utc = "2026-05-24T00:00:00+00:00"
    p_hash = _payload_hash(p)
    block_hash = _compute_block_hash(seq, event_type, trace_id, timestamp_utc, p_hash, prev_hash)

    return {
        "seq": seq,
        "event_type": event_type,
        "trace_id": trace_id,
        "timestamp_utc": timestamp_utc,
        "payload_hash": p_hash,
        "prev_hash": prev_hash,
        "block_hash": block_hash,
        "signature_hex": None,
        "public_key_hex": None,
        "payload": p,
    }


def test_verify_block_hash_valid() -> None:
    """verify_block_hash returns True for a properly formed block."""
    block = _make_block()
    assert verify_block_hash(block) is True


def test_verify_block_hash_tampered_payload() -> None:
    """verify_block_hash returns False when payload_hash doesn't match block_hash."""
    block = _make_block()
    block["payload_hash"] = "a" * 64  # tampered
    assert verify_block_hash(block) is False


def test_verify_block_hash_missing_field() -> None:
    """verify_block_hash returns False for a block missing required fields."""
    incomplete = {"seq": 1, "event_type": "ADVISORY_DECISION"}
    assert verify_block_hash(incomplete) is False


def test_verify_chain_empty_returns_ok() -> None:
    """verify_chain on empty list returns ok=True, blocks_checked=0."""
    result = verify_chain([])
    assert result.ok is True
    assert result.blocks_checked == 0
    assert result.reason == "empty_chain"


def test_verify_chain_single_block_valid() -> None:
    """verify_chain with one valid genesis block returns ok=True."""
    block = _make_block(seq=1, prev_hash="0" * 64)
    result = verify_chain([block])
    assert result.ok is True
    assert result.blocks_checked == 1
    assert result.reason == "chain_valid"


def test_verify_chain_two_blocks_valid() -> None:
    """verify_chain with two linked blocks returns ok=True."""
    b1 = _make_block(seq=1, prev_hash="0" * 64)
    b2 = _make_block(seq=2, trace_id="t002", prev_hash=b1["block_hash"])
    result = verify_chain([b1, b2])
    assert result.ok is True
    assert result.blocks_checked == 2


def test_verify_chain_broken_prev_hash() -> None:
    """verify_chain with wrong prev_hash detects chain_broken."""
    b1 = _make_block(seq=1, prev_hash="0" * 64)
    b2 = _make_block(seq=2, trace_id="t002", prev_hash="deadbeef" * 8)  # wrong prev
    result = verify_chain([b1, b2])
    assert result.ok is False
    assert result.reason == "chain_broken"
    assert result.first_broken_seq == 2


def test_verify_chain_tampered_block_hash() -> None:
    """verify_chain detects tampered block_hash (block_hash_mismatch)."""
    b1 = _make_block(seq=1, prev_hash="0" * 64)
    b1_tampered = dict(b1)
    b1_tampered["block_hash"] = "b" * 64  # tamper the hash
    result = verify_chain([b1_tampered])
    assert result.ok is False
    assert result.reason == "block_hash_mismatch"
    assert result.first_broken_seq == 1


# ── Signer tests ────────────────────────────────────────────────────────────────


def test_public_key_version_default() -> None:
    """public_key_version returns '1' when OMNI_AUDIT_KEY_VERSION is unset."""
    env_backup = os.environ.pop("OMNI_AUDIT_KEY_VERSION", None)
    try:
        v = public_key_version()
        assert v == "1"
    finally:
        if env_backup is not None:
            os.environ["OMNI_AUDIT_KEY_VERSION"] = env_backup


def test_public_key_version_from_env() -> None:
    """public_key_version returns the value from OMNI_AUDIT_KEY_VERSION env."""
    os.environ["OMNI_AUDIT_KEY_VERSION"] = "v2"
    try:
        assert public_key_version() == "v2"
    finally:
        del os.environ["OMNI_AUDIT_KEY_VERSION"]


def test_sign_block_hash_unsigned_mode() -> None:
    """sign_block_hash returns None when OMNI_AUDIT_PRIVATE_KEY_PATH is not set."""
    env_backup = os.environ.pop("OMNI_AUDIT_PRIVATE_KEY_PATH", None)
    try:
        # Must clear lru_cache to pick up env change
        from services.audit_ledger.signer import _load_private_key
        _load_private_key.cache_clear()

        result = sign_block_hash("a" * 64)
        assert result is None
    finally:
        if env_backup is not None:
            os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"] = env_backup
        from services.audit_ledger.signer import _load_private_key
        _load_private_key.cache_clear()


def test_public_key_hex_unsigned_mode() -> None:
    """public_key_hex returns None when OMNI_AUDIT_PRIVATE_KEY_PATH is not set."""
    env_backup = os.environ.pop("OMNI_AUDIT_PRIVATE_KEY_PATH", None)
    try:
        from services.audit_ledger.signer import _load_private_key
        _load_private_key.cache_clear()

        result = public_key_hex()
        assert result is None
    finally:
        if env_backup is not None:
            os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"] = env_backup
        from services.audit_ledger.signer import _load_private_key
        _load_private_key.cache_clear()


def test_load_private_key_bad_path_raises() -> None:
    """_load_private_key with a non-existent path raises AuditLedgerError."""
    os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"] = "/nonexistent/path/key.pem"
    try:
        from services.audit_ledger.signer import _load_private_key
        _load_private_key.cache_clear()

        with pytest.raises(AuditLedgerError, match="Failed to load audit private key"):
            _load_private_key()
    finally:
        del os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"]
        from services.audit_ledger.signer import _load_private_key
        _load_private_key.cache_clear()


# ── CRAT write + verify integration ─────────────────────────────────────────────


async def test_written_chain_passes_verification() -> None:
    """Blocks written by write_audit_block form a valid chain that verify_chain passes."""
    import json
    from services.audit_ledger.chain_writer import write_audit_block, REDIS_AUDIT_BLOCKS_KEY

    class _KafkaCapture:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
            self.sent.append((topic, payload))

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()

    # Write 3 blocks
    for i in range(3):
        await write_audit_block(
            event_type="ADVISORY_DECISION",
            trace_id=f"verify-test-{i:03d}",
            payload={"seq": i, "verdict": "SUGGEST_REMEDIATION"},
            redis=redis,
            kafka=kafka,
            kafka_topic="omni-audit-chain",
        )

    # Read blocks from Redis and verify the chain
    raw_blocks = await redis.lrange(REDIS_AUDIT_BLOCKS_KEY, 0, -1)
    blocks = [json.loads(b) for b in raw_blocks]

    assert len(blocks) == 3
    result = verify_chain(blocks)
    assert result.ok is True, f"Chain verification failed: {result}"
    assert result.blocks_checked == 3
