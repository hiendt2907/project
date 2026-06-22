"""Unit tests for services.audit_ledger.signer."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from services.audit_ledger.signer import (
    AuditLedgerError,
    public_key_version,
    sign_block_hash,
    public_key_hex,
)


def _make_ed25519_pem() -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PrivateFormat,
        NoEncryption,
    )
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


@pytest.fixture(autouse=True)
def clear_lru_cache():
    from services.audit_ledger import signer
    signer._load_private_key.cache_clear()
    yield
    signer._load_private_key.cache_clear()


def test_sign_returns_none_when_key_path_unset():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OMNI_AUDIT_PRIVATE_KEY_PATH", None)
        result = sign_block_hash("deadbeef" * 8)
    assert result is None


def test_public_key_hex_returns_none_when_disabled():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OMNI_AUDIT_PRIVATE_KEY_PATH", None)
        assert public_key_hex() is None


def test_public_key_version_default():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OMNI_AUDIT_KEY_VERSION", None)
        assert public_key_version() == "1"


def test_public_key_version_from_env():
    with patch.dict(os.environ, {"OMNI_AUDIT_KEY_VERSION": "42"}):
        assert public_key_version() == "42"


def test_sign_produces_hex_signature():
    pem = _make_ed25519_pem()
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem)
        key_path = f.name
    try:
        with patch.dict(os.environ, {"OMNI_AUDIT_PRIVATE_KEY_PATH": key_path}):
            block_hash = "a" * 64  # 32-byte hash hex
            sig = sign_block_hash(block_hash)
        assert sig is not None
        assert isinstance(sig, str)
        assert len(sig) == 128  # Ed25519 signature = 64 bytes = 128 hex chars
    finally:
        os.unlink(key_path)


def test_sign_is_deterministic_for_same_input():
    pem = _make_ed25519_pem()
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem)
        key_path = f.name
    try:
        with patch.dict(os.environ, {"OMNI_AUDIT_PRIVATE_KEY_PATH": key_path}):
            block_hash = "b" * 64
            sig1 = sign_block_hash(block_hash)
            sig2 = sign_block_hash(block_hash)
        assert sig1 == sig2
    finally:
        os.unlink(key_path)


def test_sign_raises_on_missing_key_file():
    with patch.dict(os.environ, {"OMNI_AUDIT_PRIVATE_KEY_PATH": "/nonexistent/key.pem"}):
        with pytest.raises(AuditLedgerError, match="Failed to load"):
            sign_block_hash("cc" * 32)


def test_public_key_hex_roundtrip():
    pem = _make_ed25519_pem()
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem)
        key_path = f.name
    try:
        with patch.dict(os.environ, {"OMNI_AUDIT_PRIVATE_KEY_PATH": key_path}):
            pub_hex = public_key_hex()
        assert pub_hex is not None
        assert len(pub_hex) == 64  # Ed25519 public key = 32 bytes
    finally:
        os.unlink(key_path)


def test_sign_block_hash_rejects_non_ed25519_key():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with patch("services.audit_ledger.signer._load_private_key", return_value=rsa_key):
        with pytest.raises(AuditLedgerError, match="not Ed25519PrivateKey"):
            sign_block_hash("aa" * 32)


def test_sign_block_hash_wraps_invalid_hex():
    pem = _make_ed25519_pem()
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem)
        key_path = f.name
    try:
        with patch.dict(os.environ, {"OMNI_AUDIT_PRIVATE_KEY_PATH": key_path}):
            with pytest.raises(AuditLedgerError, match="Ed25519 signing failed"):
                sign_block_hash("not-valid-hex")
    finally:
        os.unlink(key_path)


def test_public_key_hex_raises_on_export_failure():
    class KeyNoPub:
        def public_key(self):
            raise OSError("nope")

    with patch("services.audit_ledger.signer._load_private_key", return_value=KeyNoPub()):
        with pytest.raises(AuditLedgerError, match="Failed to export public key"):
            public_key_hex()
