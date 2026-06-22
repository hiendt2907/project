"""TDD for the post-mortem → RAG SOP rebuild parser (plan step 5).

Parses real incident post-mortems under docs/post-mortems/ into omni:rag:sop
entries. Low-signal stubs (alert=unknown AND no namespace/workload) are skipped
rather than silently ingested as noise.
"""

from __future__ import annotations

from scripts.rebuild_rag_from_postmortems import (
    has_signal,
    parse_postmortem,
)

_RICH = """# Incident Post-Mortem — state-clear-001

**Date:** 2026-06-18T03:19:55Z
**Outcome:** VERIFIED_SUCCESS

## Summary

- **Alert:** `NginxTestContainerWaitingFaultLab`
- **Namespace:** `multi-agent`
- **Workload:** `nginx-test`
- **Remediation tool:** `k8s_rollout_restart`
- **Arg keys used:** `deployment`, `namespace`

## Notes

Arg values are intentionally omitted from this record.
"""

_STUB = """# Incident Post-Mortem — tr-leg-upsert

**Date:** 2026-06-18T03:18:33Z
**Outcome:** VERIFIED_SUCCESS

## Summary

- **Alert:** `unknown`
- **Namespace:** ``
- **Workload:** ``
- **Remediation tool:** `k8s_rollout_restart`
- **Arg keys used:** `namespace`
"""


def test_parse_extracts_core_fields():
    entry = parse_postmortem(_RICH, slug="state-clear-001")
    assert entry["alert_id"] == "pm-state-clear-001"
    assert entry["alert_context"]["alertname"] == "NginxTestContainerWaitingFaultLab"
    assert entry["alert_context"]["namespace"] == "multi-agent"
    assert entry["outcome"] == "VERIFIED_SUCCESS"
    assert entry["source"] == "post-mortem"


def test_parse_remediation_tool_and_arg_keys():
    entry = parse_postmortem(_RICH, slug="state-clear-001")
    rem = entry["proposed_remediation"][0]
    assert rem["tool"] == "k8s_rollout_restart"
    assert set(rem["arg_keys"]) == {"deployment", "namespace"}
    # arg VALUES must never be ingested (only keys)
    assert "value" not in rem


def test_parse_includes_workload_in_labels():
    entry = parse_postmortem(_RICH, slug="state-clear-001")
    assert entry["alert_context"]["labels"].get("workload") == "nginx-test"


def test_has_signal_true_for_rich():
    assert has_signal(parse_postmortem(_RICH, slug="state-clear-001")) is True


def test_has_signal_false_for_stub():
    # alert=unknown + no ns + no workload → no signal, must be skipped
    assert has_signal(parse_postmortem(_STUB, slug="tr-leg-upsert")) is False


def test_parse_malformed_returns_minimal_entry_without_crash():
    entry = parse_postmortem("garbage with no fields", slug="x")
    assert entry["alert_id"] == "pm-x"
    assert has_signal(entry) is False
