"""Ed25519 signing for CRAT audit blocks (SOX §404, PCI-DSS v4.0)."""

from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

_ENV_KEY_PATH = "OMNI_AUDIT_PRIVATE_KEY_PATH"
_ENV_KEY_VERSION = "OMNI_AUDIT_KEY_VERSION"


def public_key_version() -> str:
    """Return the current key version (from env). Used to tag CRAT blocks for rotation tracing."""
    return os.environ.get(_ENV_KEY_VERSION, "1").strip() or "1"


class AuditLedgerError(Exception):
    """Raised when the audit ledger cannot be written — triggers fail-closed abort."""


@lru_cache(maxsize=1)
def _load_private_key() -> object | None:
    """Load Ed25519 private key from PEM. Returns None if OMNI_AUDIT_PRIVATE_KEY_PATH unset."""
    path = os.environ.get(_ENV_KEY_PATH, "").strip()
    if not path:
        logger.warning("event=audit_signing_disabled reason=%s_not_set", _ENV_KEY_PATH)
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        with open(path, "rb") as fh:
            key = load_pem_private_key(fh.read(), password=None)
        logger.info("event=audit_key_loaded path=%s", path)
        return key
    except Exception as exc:
        raise AuditLedgerError(f"Failed to load audit private key from {path}: {exc}") from exc


def sign_block_hash(block_hash_hex: str) -> str | None:
    """Sign block_hash with Ed25519. Returns hex signature, or None when signing is disabled."""
    key = _load_private_key()
    if key is None:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if not isinstance(key, Ed25519PrivateKey):
            raise AuditLedgerError(f"Loaded key is not Ed25519PrivateKey: {type(key)}")
        return key.sign(bytes.fromhex(block_hash_hex)).hex()
    except AuditLedgerError:
        raise
    except Exception as exc:
        raise AuditLedgerError(f"Ed25519 signing failed: {exc}") from exc


def public_key_hex() -> str | None:
    """Return the raw Ed25519 public key as hex, or None when signing is disabled."""
    key = _load_private_key()
    if key is None:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    except Exception as exc:
        raise AuditLedgerError(f"Failed to export public key: {exc}") from exc
