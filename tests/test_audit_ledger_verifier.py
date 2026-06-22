"""Tests for services.audit_ledger.verifier (hash chain + Ed25519)."""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from services.audit_ledger.verifier import (
    verify_block_hash,
    verify_block_signature,
    verify_chain,
)


def _canonical_block_fields(
    seq: int,
    event_type: str,
    trace_id: str,
    timestamp_utc: str,
    payload_hash: str,
    prev_hash: str,
) -> tuple[str, str]:
    canonical = f"{seq}|{event_type}|{trace_id}|{timestamp_utc}|{payload_hash}|{prev_hash}"
    return canonical, hashlib.sha256(canonical.encode()).hexdigest()


def test_verify_block_hash_success() -> None:
    p_hash = "a" * 64
    prev = "0" * 64
    _, bh = _canonical_block_fields(1, "ADVISORY", "t1", "2026-01-01T00:00:00Z", p_hash, prev)
    block = {
        "seq": 1,
        "event_type": "ADVISORY",
        "trace_id": "t1",
        "timestamp_utc": "2026-01-01T00:00:00Z",
        "payload_hash": p_hash,
        "prev_hash": prev,
        "block_hash": bh,
    }
    assert verify_block_hash(block) is True


def test_verify_block_hash_missing_field() -> None:
    assert verify_block_hash({"seq": 1}) is False


def test_verify_chain_empty() -> None:
    r = verify_chain([])
    assert r.ok and r.blocks_checked == 0
    assert r.reason == "empty_chain"


def test_verify_chain_hash_mismatch() -> None:
    block = {
        "seq": 1,
        "event_type": "E",
        "trace_id": "t",
        "timestamp_utc": "ts",
        "payload_hash": "p" * 64,
        "prev_hash": "0" * 64,
        "block_hash": "deadbeef" * 8,
    }
    r = verify_chain([block])
    assert r.ok is False
    assert r.reason == "block_hash_mismatch"


def test_verify_chain_broken_prev() -> None:
    p_hash = "b" * 64
    prev = "0" * 64
    _, bh = _canonical_block_fields(1, "E", "t", "ts", p_hash, prev)
    block = {
        "seq": 1,
        "event_type": "E",
        "trace_id": "t",
        "timestamp_utc": "ts",
        "payload_hash": p_hash,
        "prev_hash": prev,
        "block_hash": bh,
    }
    block2 = dict(block)
    block2["seq"] = 2
    block2["prev_hash"] = "c" * 64
    _, bh2 = _canonical_block_fields(2, "E", "t", "ts", p_hash, block2["prev_hash"])
    block2["block_hash"] = bh2
    r = verify_chain([block, block2])
    assert r.ok is False
    assert r.reason == "chain_broken"


def test_verify_block_signature_missing_sig() -> None:
    assert verify_block_signature({"block_hash": "ab"}, "00") is False


def test_verify_block_signature_invalid_hex() -> None:
    assert verify_block_signature(
        {"block_hash": "aa", "signature_hex": "not-hex"},
        "not-hex",
    ) is False


def test_verify_chain_with_valid_ed25519(tmp_path) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    (tmp_path / "k.pem").write_bytes(pem)
    pub_hex = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()

    p_hash = "d" * 64
    prev = "0" * 64
    _, bh = _canonical_block_fields(1, "E", "t", "ts", p_hash, prev)
    sig = key.sign(bytes.fromhex(bh)).hex()
    block = {
        "seq": 1,
        "event_type": "E",
        "trace_id": "t",
        "timestamp_utc": "ts",
        "payload_hash": p_hash,
        "prev_hash": prev,
        "block_hash": bh,
        "signature_hex": sig,
        "public_key_hex": pub_hex,
    }
    assert verify_chain([block]).ok is True


def test_verify_chain_signature_invalid() -> None:
    p_hash = "e" * 64
    prev = "0" * 64
    _, bh = _canonical_block_fields(1, "E", "t", "ts", p_hash, prev)
    other = ed25519.Ed25519PrivateKey.generate()
    bad_sig = other.sign(bytes.fromhex(bh)).hex()
    pub_hex = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    block = {
        "seq": 1,
        "event_type": "E",
        "trace_id": "t",
        "timestamp_utc": "ts",
        "payload_hash": p_hash,
        "prev_hash": prev,
        "block_hash": bh,
        "signature_hex": bad_sig,
        "public_key_hex": pub_hex,
    }
    r = verify_chain([block])
    assert r.ok is False
    assert r.reason == "signature_invalid"
