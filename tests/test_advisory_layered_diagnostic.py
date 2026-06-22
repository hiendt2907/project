"""Tests for the Bottom-Up Layered Diagnostic framework in advisory mode."""

from __future__ import annotations

import pytest

from pkg.reasoning.analyst_advisory_schema import VerificationStep


# ---------------------------------------------------------------------------
# Layer inference from command text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected_layer", [
    # os_baremetal patterns
    ("df -hT /var/data", "os_baremetal"),
    ("iostat -xz 1 5", "os_baremetal"),
    ("dmesg -T | tail -50", "os_baremetal"),
    ("journalctl -xe --no-pager | tail -100", "os_baremetal"),
    ("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSUSE%", "os_baremetal"),
    ("top -b -n 1 | head -20", "os_baremetal"),
    ("du -sh /var/data/* | sort -rh | head -20", "os_baremetal"),
    ("free -h", "os_baremetal"),
    # network patterns
    ("ss -tulnp | grep 6379", "network"),
    ("ip route show", "network"),
    ("mtr --report --report-cycles 5 8.8.8.8", "network"),
    ("dig redis.default.svc.cluster.local @10.96.0.10", "network"),
    ("tcpdump -i any -c 50 port 443", "network"),
    # kubernetes patterns
    ("kubectl get pods -n default -o wide", "kubernetes"),
    ("kubectl describe pod my-pod -n kube-system", "kubernetes"),
    ("kubectl logs my-pod -n default --tail=100", "kubernetes"),
    # prometheus patterns
    ("rate(container_cpu_usage_seconds_total[5m])", "prometheus"),
    ("predict_linear(node_filesystem_avail_bytes[1h], 3600)", "prometheus"),
    ("irate(http_requests_total[2m])", "prometheus"),
])
def test_layer_inferred_from_command(command: str, expected_layer: str) -> None:
    step = VerificationStep(order=1, command=command, rationale="test")
    assert step.layer == expected_layer, (
        f"command={command!r}: expected layer={expected_layer!r}, got {step.layer!r}"
    )


def test_explicit_layer_not_overridden() -> None:
    """An explicitly set non-default layer must not be overwritten."""
    step = VerificationStep(
        order=1,
        layer="network",
        command="kubectl get pods",  # looks like k8s but layer is explicit
        rationale="checking network from pod context",
    )
    assert step.layer == "network"


def test_default_layer_fallback_for_unknown_command() -> None:
    step = VerificationStep(order=1, command="echo hello", rationale="warmup")
    assert step.layer == "kubernetes"  # safe default


# ---------------------------------------------------------------------------
# Schema validation: layer is preserved round-trip
# ---------------------------------------------------------------------------

def test_verification_step_dict_roundtrip() -> None:
    raw = {
        "order": 1,
        "layer": "os_baremetal",
        "command": "df -hT /var/data",
        "expected_output": "Use% < 80%",
        "rationale": "Check host partition",
    }
    step = VerificationStep(**raw)
    assert step.layer == "os_baremetal"
    assert step.order == 1


def test_verification_step_missing_layer_gets_inferred() -> None:
    """LLM response missing 'layer' must not raise — it should be inferred."""
    raw = {
        "order": 2,
        "command": "df -hT /var/data",
        "rationale": "Check partition fill",
    }
    step = VerificationStep(**raw)
    assert step.layer == "os_baremetal"


# ---------------------------------------------------------------------------
# Telegram rendering includes layer badges
# ---------------------------------------------------------------------------

def test_telegram_render_shows_layer_badge() -> None:
    from workers.telegram_advisory_emitter import _render_verification_steps

    steps = [
        VerificationStep(order=1, layer="os_baremetal", command="df -hT /var/data", rationale="Check disk"),
        VerificationStep(order=2, layer="kubernetes", command="kubectl get pods -n default", rationale="Check pods"),
    ]
    rendered = _render_verification_steps(steps)
    assert "[L1 — OS]" in rendered, "os_baremetal badge missing"
    assert "[L3 — K8s]" in rendered, "kubernetes badge missing"
    assert "df -hT /var/data" in rendered
    assert "kubectl get pods" in rendered


def test_telegram_render_network_layer_badge() -> None:
    from workers.telegram_advisory_emitter import _render_verification_steps

    steps = [VerificationStep(order=1, layer="network", command="ss -tulnp", rationale="Check sockets")]
    rendered = _render_verification_steps(steps)
    assert "[L2 — Network]" in rendered


def test_telegram_render_prometheus_layer_badge() -> None:
    from workers.telegram_advisory_emitter import _render_verification_steps

    steps = [VerificationStep(order=1, layer="prometheus", command="rate(cpu[5m])", rationale="CPU rate")]
    rendered = _render_verification_steps(steps)
    assert "[L4 — Prometheus]" in rendered
