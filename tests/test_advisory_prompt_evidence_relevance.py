"""Regression guard: the advisory system prompt must forbid citing evidence that does
not share the alert's subject (e.g. a cluster-wide CPU/mem baseline for a self-monitoring
KPI alert), and must give explicit handling for Omni's own meta_self alert family.

Root cause this closes: classify_alert() flagged OmniAdvisoryAcceptanceRateLow as
meta_self (no cluster target), but the prompt told the LLM to always anchor claims to
"concrete" batch evidence without requiring that evidence be ABOUT the alert's subject.
The only concrete number in the batch was an irrelevant cluster-wide 3-sigma CPU baseline,
so the LLM cited it and fabricated a "CPU saturation" root cause for a KPI-rate alert.
"""

from __future__ import annotations

from workers.advisory_mode_system_prompt import build_advisory_system_prompt


def test_prompt_requires_evidence_to_share_alert_subject() -> None:
    prompt = build_advisory_system_prompt()
    assert "EVIDENCE RELEVANCE" in prompt
    assert "IRRELEVANT" in prompt
    assert "UNDER-EVIDENCED" in prompt


def test_prompt_has_explicit_meta_self_alert_handling() -> None:
    prompt = build_advisory_system_prompt()
    assert "SELF-MONITORING / META ALERTS" in prompt
    assert "OmniAdvisoryAcceptanceRateLow" in prompt
    assert "no cluster remediation" in prompt or "không có workload cluster" in prompt


def test_prompt_forbids_borrowing_unrelated_resource_baseline() -> None:
    prompt = build_advisory_system_prompt()
    assert "NEVER repurpose a CPU/memory/disk number" in prompt
