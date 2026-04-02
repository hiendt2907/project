"""memory_normalize: canonical text, strip args, fingerprint."""

from __future__ import annotations

from execution.memory_normalize import (
    canonical_symptom_text,
    extract_workload_fingerprint,
    stable_playbook_pattern_key,
    strip_ephemeral_from_args,
)


def test_canonical_symptom_collapses_whitespace() -> None:
    assert canonical_symptom_text("  Hello  World  ", strip_pods=False) == "hello world"


def test_strip_ephemeral_removes_pod_name() -> None:
    d = strip_ephemeral_from_args({"pod_name": "x", "namespace": "ns"})
    assert d["pod_name"] == "<ephemeral>"
    assert d["namespace"] == "ns"


def test_fingerprint_stable_for_ns_dep() -> None:
    t = "namespace: multi-agent deployment: nginx crash"
    a = extract_workload_fingerprint(t)
    b = extract_workload_fingerprint(t)
    assert a == b
    assert len(a) == 24


def test_pattern_key_stable() -> None:
    k1 = stable_playbook_pattern_key("redis_health", "check redis", {})
    k2 = stable_playbook_pattern_key("redis_health", "check redis", {})
    assert k1 == k2
