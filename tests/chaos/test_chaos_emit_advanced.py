"""Unit chaos tests — advanced emit paths: HITL, store_autonomous_trace_context, and signer.

Covers:
- emit_hitl_pending advisory-mode kill-switch (early return path)
- emit_hitl_pending happy path with mocked HITL routing enabled
- store_autonomous_trace_context Redis snapshot
- signer.py with a real Ed25519 key (signing + public key export)
- verifier.py verify_block_signature with real key
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from services.audit_ledger.signer import AuditLedgerError


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))

    async def send_envelope_inner(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))

    async def close(self) -> None:
        pass


def _make_ctx(redis=None, kafka=None, **settings_overrides) -> SimpleNamespace:
    base = {
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_audit_chain": "omni-audit-chain",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "omni_auto_execute_enabled": False,
        "omni_siem_suggest_only": True,
        "omni_hitl_routing_enabled": False,
    }
    base.update(settings_overrides)
    return SimpleNamespace(
        redis=redis or fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=kafka or _KafkaCapture(),
        settings=SimpleNamespace(**base),
    )


# ── emit_hitl_pending ─────────────────────────────────────────────────────────


async def test_emit_hitl_pending_advisory_mode_blocks_silently() -> None:
    """emit_hitl_pending returns without dispatch when advisory mode blocks HITL."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    kafka = _KafkaCapture()
    ctx = _make_ctx(kafka=kafka, omni_auto_execute_enabled=False, omni_hitl_routing_enabled=False)

    await emit_hitl_pending(
        ctx,
        trace="hitl-blocked-001",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "nginx-lab"},
    )

    # In advisory mode HITL is blocked — nothing dispatched
    hitl_msgs = [t for t, _ in kafka.sent if t == "omni-hitl-pending"]
    assert len(hitl_msgs) == 0


async def test_emit_hitl_pending_no_settings_returns() -> None:
    """emit_hitl_pending returns immediately when ctx.settings is None."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    ctx = SimpleNamespace(
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=_KafkaCapture(),
        settings=None,
    )

    # Must not raise
    await emit_hitl_pending(
        ctx,
        trace="hitl-no-settings-001",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent"},
    )


async def test_emit_hitl_pending_hitl_routing_enabled_dispatches() -> None:
    """emit_hitl_pending with hitl_routing_enabled=True dispatches to omni-hitl-pending."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _make_ctx(
        redis=redis,
        kafka=kafka,
        omni_auto_execute_enabled=False,
        omni_hitl_routing_enabled=True,  # allows HITL dispatch
    )

    with patch("workers.evidence_mutate_emit.write_audit_block"):
        await emit_hitl_pending(
            ctx,
            trace="hitl-dispatch-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
            hitl_reason="siem_critical_action",
            explain="nginx-lab is in CrashLoop",
            advise="restart to clear OOM state",
        )

    hitl_msgs = [p for t, p in kafka.sent if t == "omni-hitl-pending"]
    assert len(hitl_msgs) == 1

    # Verify HITL metadata present in payload
    body = json.loads(hitl_msgs[0]["data"])
    assert body["hitl_pending"] is True
    assert body["hitl_reason"] == "siem_critical_action"
    assert "explain" in body
    assert "advise" in body


async def test_emit_hitl_pending_crat_fail_closed() -> None:
    """emit_hitl_pending aborts silently when CRAT write fails."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    kafka = _KafkaCapture()
    ctx = _make_ctx(kafka=kafka, omni_hitl_routing_enabled=True)

    with patch(
        "workers.evidence_mutate_emit.write_audit_block",
        side_effect=AuditLedgerError("redis down"),
    ):
        await emit_hitl_pending(
            ctx,
            trace="hitl-crat-fail-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent"},
        )

    # CRAT failed → no omni-hitl-pending dispatch
    assert not any(t == "omni-hitl-pending" for t, _ in kafka.sent)


async def test_emit_hitl_pending_missing_redis_returns() -> None:
    """emit_hitl_pending with redis=None aborts without exception."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    ctx = SimpleNamespace(
        redis=None,
        kafka=_KafkaCapture(),
        settings=SimpleNamespace(
            kafka_topic_actions="omni-actions",
            kafka_topic_audit_chain="omni-audit-chain",
            kafka_topic_hitl_pending="omni-hitl-pending",
            omni_auto_execute_enabled=False,
            omni_hitl_routing_enabled=True,
        ),
    )

    await emit_hitl_pending(
        ctx,
        trace="hitl-no-redis-001",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent"},
    )


# ── store_autonomous_trace_context ────────────────────────────────────────────


async def test_store_autonomous_trace_context_basic() -> None:
    """store_autonomous_trace_context writes a snapshot to Redis with TTL 7200s."""
    from workers.evidence_mutate_emit import store_autonomous_trace_context

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    await store_autonomous_trace_context(
        redis,
        "ctx-test-001",
        sanitized_text="nginx-lab CrashLoopBackOff: OOM killed after memory limit",
    )

    raw = await redis.get("omni:autonomous:ctx:ctx-test-001")
    assert raw is not None
    payload = json.loads(raw)
    assert "sanitized_text" in payload
    assert "nginx-lab" in payload["sanitized_text"]

    ttl = await redis.ttl("omni:autonomous:ctx:ctx-test-001")
    assert 0 < ttl <= 7200


async def test_store_autonomous_trace_context_with_batch() -> None:
    """store_autonomous_trace_context stores batch-derived fields when batch provided."""
    from workers.evidence_mutate_emit import store_autonomous_trace_context

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    batch = [
        {
            "probe": "cpu_utilization",
            "symptom_group": "RESOURCE_EXHAUSTION",
            "alert_hint": "HighCPU millicore limit",
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "alertname": "KubeContainerHighCPUUsage",
                    "namespace": "multi-agent",
                    "deployment": "nginx-lab",
                }
            }),
        }
    ]

    await store_autonomous_trace_context(
        redis,
        "ctx-batch-001",
        batch=batch,
        sanitized_text="CPU spike on nginx-lab",
    )

    raw = await redis.get("omni:autonomous:ctx:ctx-batch-001")
    assert raw is not None
    payload = json.loads(raw)
    # verify_probe_ids should be populated
    assert "batch_preview" in payload
    assert "omni.io/incident-id" in payload
    assert payload["omni.io/incident-id"] == "ctx-batch-001"


async def test_store_autonomous_trace_context_exception_swallowed() -> None:
    """store_autonomous_trace_context swallows exceptions gracefully."""
    from workers.evidence_mutate_emit import store_autonomous_trace_context

    # Pass a broken redis-like object
    class _BrokenRedis:
        async def setex(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    # Must not raise
    await store_autonomous_trace_context(
        _BrokenRedis(),
        "ctx-error-001",
        sanitized_text="test",
    )


# ── Signer with real Ed25519 key ──────────────────────────────────────────────


def _generate_ed25519_pem() -> bytes:
    """Generate a temporary Ed25519 PEM private key for testing."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key = Ed25519PrivateKey.generate()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def test_sign_block_hash_with_real_key() -> None:
    """sign_block_hash returns a hex signature when a valid Ed25519 key is configured."""
    from services.audit_ledger.signer import sign_block_hash, _load_private_key

    pem = _generate_ed25519_pem()
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem)
        key_path = f.name

    os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"] = key_path
    _load_private_key.cache_clear()
    try:
        block_hash = "a" * 64
        sig = sign_block_hash(block_hash)
        assert sig is not None
        assert isinstance(sig, str)
        assert len(sig) == 128  # Ed25519 signature is 64 bytes = 128 hex chars
    finally:
        del os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"]
        _load_private_key.cache_clear()
        os.unlink(key_path)


def test_public_key_hex_with_real_key() -> None:
    """public_key_hex returns hex string when a valid Ed25519 key is configured."""
    from services.audit_ledger.signer import public_key_hex, _load_private_key

    pem = _generate_ed25519_pem()
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem)
        key_path = f.name

    os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"] = key_path
    _load_private_key.cache_clear()
    try:
        pub = public_key_hex()
        assert pub is not None
        assert isinstance(pub, str)
        assert len(pub) == 64  # Ed25519 public key is 32 bytes = 64 hex chars
    finally:
        del os.environ["OMNI_AUDIT_PRIVATE_KEY_PATH"]
        _load_private_key.cache_clear()
        os.unlink(key_path)


# ── verify_block_signature with real key ─────────────────────────────────────


def test_verify_block_signature_with_real_key() -> None:
    """verify_block_signature returns True for a correctly signed block."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from services.audit_ledger.verifier import verify_block_signature

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    block_hash = "deadbeef" * 8  # 64 hex chars
    sig_hex = priv_key.sign(bytes.fromhex(block_hash)).hex()

    block = {
        "block_hash": block_hash,
        "signature_hex": sig_hex,
        "public_key_hex": pub_hex,
    }

    assert verify_block_signature(block, pub_hex) is True


def test_verify_block_signature_wrong_key() -> None:
    """verify_block_signature returns False when the public key doesn't match the signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from services.audit_ledger.verifier import verify_block_signature

    priv_key1 = Ed25519PrivateKey.generate()
    priv_key2 = Ed25519PrivateKey.generate()  # different key
    pub_hex2 = priv_key2.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    block_hash = "cafebabe" * 8
    sig_hex = priv_key1.sign(bytes.fromhex(block_hash)).hex()

    block = {
        "block_hash": block_hash,
        "signature_hex": sig_hex,
    }

    # Verify with wrong public key → False
    assert verify_block_signature(block, pub_hex2) is False


def test_verify_block_signature_no_sig_hex() -> None:
    """verify_block_signature returns False when block has no signature_hex."""
    from services.audit_ledger.verifier import verify_block_signature

    block = {"block_hash": "a" * 64}
    assert verify_block_signature(block, "b" * 64) is False
