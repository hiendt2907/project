"""Audit chain integrity verification — hash-chain + Ed25519 signature checks."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64


@dataclass
class VerifyResult:
    ok: bool
    blocks_checked: int
    first_broken_seq: int | None
    reason: str
    errors: list[str] = field(default_factory=list)


def verify_block_hash(block: dict) -> bool:
    """Recompute block_hash from fields and compare to stored value."""
    try:
        seq = block["seq"]
        event_type = block["event_type"]
        trace_id = block["trace_id"]
        timestamp_utc = block["timestamp_utc"]
        p_hash = block["payload_hash"]
        prev_hash = block["prev_hash"]
        stored_hash = block["block_hash"]

        canonical = f"{seq}|{event_type}|{trace_id}|{timestamp_utc}|{p_hash}|{prev_hash}"
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        return expected == stored_hash
    except KeyError as exc:
        logger.warning("event=verify_block_hash_missing_field err=%s", exc)
        return False


def verify_block_signature(block: dict, pub_key_hex: str) -> bool:
    """Verify Ed25519 signature on block_hash using the provided public key hex."""
    sig_hex = block.get("signature_hex")
    if not sig_hex:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        pub_bytes = bytes.fromhex(pub_key_hex)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(bytes.fromhex(sig_hex), bytes.fromhex(block["block_hash"]))
        return True
    except Exception as exc:
        logger.warning("event=verify_signature_failed err=%s", exc)
        return False


def verify_chain(blocks: list[dict]) -> VerifyResult:
    """Verify entire chain: hash continuity + (optionally) signatures.

    Each block's prev_hash must equal the prior block's block_hash.
    Signature verification runs if signature_hex is present.
    """
    if not blocks:
        return VerifyResult(ok=True, blocks_checked=0, first_broken_seq=None, reason="empty_chain")

    errors: list[str] = []
    expected_prev = _GENESIS_HASH

    for i, block in enumerate(blocks):
        seq = block.get("seq", i)

        # Hash integrity
        if not verify_block_hash(block):
            errors.append(f"seq={seq}: block_hash mismatch")
            return VerifyResult(
                ok=False,
                blocks_checked=i,
                first_broken_seq=seq,
                reason="block_hash_mismatch",
                errors=errors,
            )

        # Chain continuity
        if block.get("prev_hash") != expected_prev:
            errors.append(f"seq={seq}: prev_hash chain broken")
            return VerifyResult(
                ok=False,
                blocks_checked=i,
                first_broken_seq=seq,
                reason="chain_broken",
                errors=errors,
            )

        # Signature (only when present)
        sig_hex = block.get("signature_hex")
        pub_hex = block.get("public_key_hex")
        if sig_hex and pub_hex:
            if not verify_block_signature(block, pub_hex):
                errors.append(f"seq={seq}: signature invalid")
                return VerifyResult(
                    ok=False,
                    blocks_checked=i,
                    first_broken_seq=seq,
                    reason="signature_invalid",
                    errors=errors,
                )

        expected_prev = block["block_hash"]

    return VerifyResult(
        ok=True,
        blocks_checked=len(blocks),
        first_broken_seq=None,
        reason="chain_valid",
        errors=errors,
    )
