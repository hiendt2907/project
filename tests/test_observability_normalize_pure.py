"""Pure tests for observability.normalize (no unittest.mock)."""

from __future__ import annotations

import pytest

from observability.normalize import (
    Action,
    ErrorSignature,
    Resource,
    build_canonical_query,
    canonical_query_from_rule_name,
    infer_error_hint_from_promql,
    is_clean,
    redact,
)


def test_redact_bearer_and_password_idempotent() -> None:
    raw = "Authorization: Bearer abcdefghijklmnopqrs1234567890"
    once = redact(raw)
    assert "Bearer" in once or "[REDACTED" in once
    twice = redact(once)
    assert twice == once


def test_redact_connection_string() -> None:
    # Password with hyphen avoids gitleaks omni-postgres-dsn (word-char-only rule).
    s = redact("connect via postgres://user:fake-pass@db.example:5432/app")
    assert "[REDACTED_CREDS]" in s
    assert "fake-pass" not in s


def test_redact_aws_key_id_placeholder() -> None:
    # Synthetic AKIA-shaped id (not a real AWS key).
    s = redact("key=AKIA0123456789ABCDEF")
    assert "[REDACTED_AWS_KEY_ID]" in s
    assert "AKIA0123456789ABCDEF" not in s


def test_is_clean_negative() -> None:
    assert is_clean("password=notokvaluehere") is False


def test_is_clean_positive() -> None:
    assert is_clean("pod/nginx-abc in namespace default") is True


def test_build_canonical_query_enums_and_separator() -> None:
    q = build_canonical_query(
        action=Action.CHECK,
        resource=Resource.POD,
        error_sig=ErrorSignature.OOM_KILLED,
    )
    assert q == "CHECK POD OOM_KILLED"
    q2 = build_canonical_query(
        action="scale",
        resource="deployment",
        error_sig="pending",
        separator="|",
    )
    assert q2 == "SCALE|DEPLOYMENT|PENDING"


def test_infer_error_hint_from_promql() -> None:
    assert infer_error_hint_from_promql("kube_pod_crashloopbackoff_reason") == "crash_loop_backoff"
    assert infer_error_hint_from_promql("kube_pod_imagepullbackoff") == "image_pull"
    assert infer_error_hint_from_promql("container_oom_killed_total") == "oom_killed"
    assert infer_error_hint_from_promql("container_cpu_cfs_throttled_seconds_total") == "cpu_throttle"
    assert infer_error_hint_from_promql("up") == "metric_anomaly"


@pytest.mark.parametrize(
    ("rule", "target", "hint", "promql", "needle"),
    [
        ("KubePodCrashLoopBackOff", "redis-0", "", "", "RESTART"),
        ("HighCPUThrottle", "pod-x", "", "container_cpu_cfs_throttled", "CPU_THROTTLE"),
        ("PrometheusProactiveThreshold", "", "", "kube_pod_info", "POD"),
        ("SomeDiskFullAlert", "", "disk pressure", "", "DISK_FULL"),
        ("UnknownMetricThing", "", "", "", "UNKNOWN"),
    ],
)
def test_canonical_query_from_rule_name_branches(
    rule: str,
    target: str,
    hint: str,
    promql: str,
    needle: str,
) -> None:
    out = canonical_query_from_rule_name(rule, target=target, error_hint=hint, promql_context=promql)
    assert needle in out


# ── Additional branches not yet covered ──────────────────────────────────────

def test_infer_error_hint_pending():
    from observability.normalize import infer_error_hint_from_promql
    assert infer_error_hint_from_promql("kube_pod_pending_phase") == "pending"


def test_infer_error_hint_not_ready():
    from observability.normalize import infer_error_hint_from_promql
    assert infer_error_hint_from_promql("kube_pod_not_ready_total") == "not_ready"


def test_infer_error_hint_image_pull_errimagepull():
    from observability.normalize import infer_error_hint_from_promql
    assert infer_error_hint_from_promql("ErrImagePull") == "image_pull"


@pytest.mark.parametrize(
    ("rule", "target", "hint", "promql", "needle"),
    [
        # Action branches
        ("ScaleDeploymentHPA", "deployment/api", "", "", "SCALE"),
        ("DrainNodeForMaintenance", "node-1", "", "", "DRAIN"),
        ("RollbackDeployment", "api", "", "", "ROLLBACK"),
        # Resource branches
        ("HighRedisLatency", "redis-master", "", "", "REDIS"),
        ("PgVectorIndexLag", "pgvector-svc", "", "", "PGVECTOR"),
        ("NodePressure", "node/worker-1", "", "", "NODE"),
        ("DeploymentReplicas", "deployment/api", "", "", "DEPLOYMENT"),
        ("IngressErrorRate", "ingress-nginx", "", "", "INGRESS"),
        ("KubeNamespaceQuota", "default", "", "", "NAMESPACE"),
        ("VolumeFullAlert", "pvc-data", "", "", "VOLUME"),
        ("ServiceEndpointDown", "svc/api", "", "", "SERVICE"),
        # PrometheusProactive with deployment
        ("PrometheusProactiveThreshold", "", "", "kube_deployment_available_replicas", "DEPLOYMENT"),
        # PrometheusProactive with node
        ("PrometheusProactiveThreshold", "", "", "node_cpu_seconds_total", "NODE"),
    ],
)
def test_canonical_query_resource_action_branches(
    rule: str, target: str, hint: str, promql: str, needle: str,
) -> None:
    out = canonical_query_from_rule_name(rule, target=target, error_hint=hint, promql_context=promql)
    assert needle in out
