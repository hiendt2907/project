"""Tests slice reasoning: Observe → Expectation → Probe → Compare → Finding.

Hành vi Senior SRE: thấy nginx → KỲ VỌNG 80/443 (tri thức tiên nghiệm), probe,
so sánh thực-tế-vs-kỳ-vọng → Finding. Kỳ vọng KHÔNG được thì sinh câu hỏi
(never assume), không tự bịa. Expectation = Hypothesis (predicted_evidence);
Compare = Finding — KHÔNG noun mới.
"""
from __future__ import annotations

import pytest

from aoip.capabilities.inspect_host import inspect_host
from aoip.capability import CapabilityState
from aoip.discovery_backend import VMProfileDiscoveryBackend
from aoip.objects import Finding
from aoip.service_knowledge import expected_ports
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def _ctx(profile: dict, host: str = "web-01") -> UnderstandingContext:
    return UnderstandingContext(
        host=host,
        scope=f"acme/{host}",
        backend=VMProfileDiscoveryBackend(profile),
        capability=CapabilityState(capability_id="inspect_host", scope=f"acme/{host}"),
        model=SystemModel(scope=f"acme/{host}"),
    )


def test_expected_ports_knowledge_lookup():
    assert set(expected_ports("nginx")) == {80, 443}
    assert set(expected_ports("mariadbd")) == {3306}  # normalize base
    assert expected_ports("totally-unknown-daemon") == ()


async def test_met_expectation_becomes_confirmed_finding_and_fact():
    # nginx kỳ vọng 80+443; cả hai đều listen → MET.
    profile = {
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}, {"port": 443, "service": "nginx"}],
    }
    ctx = _ctx(profile)
    await inspect_host(ctx)

    met = [f for f in ctx.findings if f.verdict]
    assert any("443" in f.claim for f in met)
    ports = {f.obj for f in ctx.model.facts if f.predicate == "exposes_port"}
    assert {"80", "443"} <= ports


async def test_unmet_expectation_becomes_finding_and_question_not_assumption():
    # nginx kỳ vọng 443 nhưng KHÔNG listen → Finding verdict False + câu hỏi.
    profile = {
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}],
    }
    ctx = _ctx(profile)
    await inspect_host(ctx)

    unmet = [f for f in ctx.findings if not f.verdict]
    assert any("443" in f.claim for f in unmet)
    # Không tự bịa: không có Fact exposes_port 443.
    assert "443" not in {f.obj for f in ctx.model.facts if f.predicate == "exposes_port"}
    # Kỳ vọng hụt → câu hỏi cho người.
    assert any("443" in c.blocking_unknown for c in ctx.communications)


async def test_findings_reference_observation_provenance():
    profile = {
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}],
    }
    ctx = _ctx(profile)
    await inspect_host(ctx)
    assert ctx.findings  # có Finding
    assert all(isinstance(f, Finding) and f.references for f in ctx.findings)


async def test_no_expectation_for_unknown_service_no_false_finding():
    # service không có tri thức kỳ vọng → KHÔNG sinh Finding bịa.
    profile = {
        "hostname": "x",
        "services": [{"name": "weird-daemon", "status": "running"}],
        "listeners": [{"port": 7000, "service": "weird-daemon"}],
    }
    ctx = _ctx(profile, host="x")
    await inspect_host(ctx)
    assert ctx.findings == []
