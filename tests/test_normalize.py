"""Unit tests for src/observability/normalize.py — redaction + canonical query."""

from __future__ import annotations

import pytest

from observability.normalize import (
    Action,
    CanonicalQuery,
    ErrorSignature,
    Resource,
    build_canonical_query,
    canonical_query_from_rule_name,
    is_clean,
    redact,
)


# ---------------------------------------------------------------------------
# Redaction tests
# ---------------------------------------------------------------------------


class TestRedact:
    def test_jwt_token_removed(self) -> None:
        jwt = (
            "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiJ1c2VyMTIzIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ"
        )
        result = redact(jwt)
        assert "[REDACTED" in result
        # Original JWT body must not survive
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in result

    def test_password_env_removed(self) -> None:
        text = "DB_PASSWORD=sUp3rS3cr3t! host=localhost"
        result = redact(text)
        assert "sUp3rS3cr3t" not in result
        assert "[REDACTED_SECRET]" in result

    def test_connection_string_removed(self) -> None:
        conn = "postgres://admin:hunter2@db.internal:5432/mydb"
        result = redact(conn)
        assert "hunter2" not in result
        assert "[REDACTED_CREDS]" in result

    def test_redis_connection_string_removed(self) -> None:
        conn = "redis://:mysecretpassword@redis:6379/0"
        result = redact(conn)
        assert "mysecretpassword" not in result

    def test_aws_key_id_removed(self) -> None:
        text = "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED_AWS_KEY_ID]" in result

    def test_api_key_removed(self) -> None:
        text = "client_secret=abcdef1234567890abcdef12"
        result = redact(text)
        assert "abcdef1234567890abcdef12" not in result

    def test_private_key_block_removed(self) -> None:
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xHn/ygMXZCqfMD5Iu7JE\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = redact(text)
        assert "MIIEpAIBAAKCAQEA" not in result
        assert "[REDACTED_PRIVATE_KEY]" in result

    def test_plain_pod_name_untouched(self) -> None:
        text = "pod=omni-worker-7d9b4c-xzk9q namespace=multi-agent"
        result = redact(text)
        assert result == text  # nothing to redact

    def test_prometheus_metric_value_untouched(self) -> None:
        text = "container_cpu_usage_seconds_total{pod='abc', ns='default'} 0.0042"
        result = redact(text)
        assert result == text

    def test_is_clean_true_for_safe_text(self) -> None:
        assert is_clean("pod-name namespace cpu=0.8") is True

    def test_is_clean_false_for_secret(self) -> None:
        assert is_clean("password=hunter2") is False

    def test_is_clean_false_for_aws_key(self) -> None:
        assert is_clean("AKIAIOSFODNN7EXAMPLE") is False

    def test_idempotent(self) -> None:
        """Calling redact twice should not change result."""
        dirty = "password=abc123 host=db"
        once = redact(dirty)
        twice = redact(once)
        assert once == twice

    def test_bearer_token_in_yaml(self) -> None:
        yaml_snippet = (
            "auth:\n"
            "  token: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiJ1In0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
            "  namespace: default"
        )
        result = redact(yaml_snippet)
        assert "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" not in result
        assert "namespace: default" in result  # safe context preserved


# ---------------------------------------------------------------------------
# Canonical Query tests
# ---------------------------------------------------------------------------


class TestBuildCanonicalQuery:
    def test_enum_values_produce_correct_string(self) -> None:
        result = build_canonical_query(
            action=Action.CHECK,
            resource=Resource.REDIS,
            error_sig=ErrorSignature.OOM_KILLED,
        )
        assert result == "CHECK REDIS OOM_KILLED"

    def test_string_values_normalised_uppercase(self) -> None:
        result = build_canonical_query(
            action="diagnose",
            resource="pod",
            error_sig="crash_loop",
        )
        assert result == "DIAGNOSE POD CRASH_LOOP"

    def test_custom_separator(self) -> None:
        result = build_canonical_query(
            action=Action.RESTART,
            resource=Resource.DEPLOYMENT,
            error_sig=ErrorSignature.CRASH_LOOP,
            separator="|",
        )
        assert result == "RESTART|DEPLOYMENT|CRASH_LOOP"

    def test_canonical_query_namedtuple(self) -> None:
        cq = CanonicalQuery(action="CHECK", resource="NODE", error_sig="HIGH_LOAD")
        assert cq.to_embed_string() == "CHECK NODE HIGH_LOAD"
        assert cq.to_embed_string(separator=" | ") == "CHECK | NODE | HIGH_LOAD"

    def test_deterministic_for_same_inputs(self) -> None:
        a = build_canonical_query(
            action=Action.DIAGNOSE, resource=Resource.POD, error_sig=ErrorSignature.PENDING
        )
        b = build_canonical_query(
            action="DIAGNOSE", resource="POD", error_sig="PENDING"
        )
        assert a == b

    def test_whitespace_stripped(self) -> None:
        result = build_canonical_query(
            action="  CHECK  ", resource="  NODE  ", error_sig="  HIGH_LOAD  "
        )
        assert result == "CHECK NODE HIGH_LOAD"


class TestCanonicalQueryFromRuleName:
    def test_oom_killed_pod(self) -> None:
        result = canonical_query_from_rule_name("KubePodOOMKilled", target="redis-0")
        assert "OOM_KILLED" in result
        assert "REDIS" in result

    def test_crashloop_pod(self) -> None:
        result = canonical_query_from_rule_name("KubePodCrashLoopBackOff", target="omni-worker-abc")
        assert "CRASH_LOOP" in result
        assert "RESTART" in result

    def test_crashloop_backoff_exact_embed_string(self) -> None:
        """Acceptance: khớp ingest/query itops_sop_ledger_v2 taxonomy."""
        result = canonical_query_from_rule_name("KubePodCrashLoopBackOff", target="omni-worker-abc")
        assert result == "RESTART POD CRASH_LOOP"

    def test_node_high_load(self) -> None:
        result = canonical_query_from_rule_name("NodeHighLoad", target="node-1")
        assert "NODE" in result
        assert "HIGH_LOAD" in result

    def test_replica_mismatch(self) -> None:
        result = canonical_query_from_rule_name("DeploymentReplicaMismatch", target="omni-worker")
        assert "REPLICA_MISMATCH" in result
        assert "DEPLOYMENT" in result

    def test_unknown_rule_falls_back_gracefully(self) -> None:
        result = canonical_query_from_rule_name("SomethingWeird")
        # Should not raise; should return a valid non-empty string
        assert len(result) > 0
        parts = result.split(" ")
        assert len(parts) == 3

    def test_disk_full_volume(self) -> None:
        result = canonical_query_from_rule_name("PersistentVolumeFillingUp", target="data-pvc")
        assert "DISK_FULL" in result or "VOLUME" in result

    def test_dns_fail(self) -> None:
        result = canonical_query_from_rule_name("DNSResolutionError")
        assert "DNS_FAIL" in result

    def test_network_timeout(self) -> None:
        result = canonical_query_from_rule_name("NetworkTimeout", error_hint="connection timeout")
        assert "NETWORK_TIMEOUT" in result
