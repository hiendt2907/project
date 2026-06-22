"""Worker pure-path coverage with real functions and payload dictionaries only."""

from __future__ import annotations

import json

import pytest

from workers import alert_sdk_truth_compare as truth
from workers import autonomous_decider
from workers import model_routing
from workers import observation_sanitize
from workers import proactive_policy_gate
from workers import promql_presets
from workers import routing_policy
from workers import tools
from workers import vm_slot_accumulation as vm_slots
from workers.tool_backend import RegistryToolBackend


def test_model_routing_classification_and_dispatch() -> None:
    assert model_routing.classify_route("forecast memory next 24h") == "heavy"
    assert model_routing.classify_route("why did the pod crash?") == "reasoning"
    assert model_routing.classify_route("hello") == "default"
    assert (
        model_routing.dispatch_task(
            model_default="d",
            model_reasoning="r",
            model_heavy="h",
            user_text="hello",
            attempt=1,
            json_parse_failures=2,
        )
        == "h"
    )


def test_observation_sanitize_redacts_known_secret_shapes() -> None:
    raw = "\n".join(
        [
            "password=super-secret",
            "Authorization: Bearer token-value",
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        ]
    )
    out = observation_sanitize.sanitize_for_llm(raw)
    assert "super-secret" not in out
    assert "token-value" not in out
    assert "Authorization: Bearer [REDACTED]" in out
    assert "PRIVATE KEY" not in out
    assert observation_sanitize.sanitize_for_llm("") == ""


@pytest.mark.asyncio
async def test_registry_tool_backend_uses_real_echo_tool() -> None:
    out = await RegistryToolBackend().invoke(None, "echo", {"msg": "hello"})
    assert out == "hello"


def test_tools_payload_and_unknown_tool_feedback() -> None:
    payload = tools.ToolCallPayload(tool="echo")
    assert payload.args == {}
    feedback = tools.format_unknown_tool_feedback_en("made_up_tool", unattended=True)
    assert "made_up_tool" in feedback
    assert "`reply`" not in feedback


def test_routing_policy_read_only_allowlist_default() -> None:
    assert routing_policy.shell_fast_path_enabled(None) is False
    assert routing_policy.is_fast_path_auto_allowed("echo", None)
    assert not routing_policy.is_fast_path_auto_allowed("execute_shell_command", None)


def test_proactive_policy_gate_wilson_lower_bound() -> None:
    assert proactive_policy_gate.wilson_lower_bound(0, 0) == 0.0
    low = proactive_policy_gate.wilson_lower_bound(1, 10)
    high = proactive_policy_gate.wilson_lower_bound(9, 10)
    assert 0.0 <= low < high < 1.0


def test_autonomous_decider_pure_prompt_and_parse_paths() -> None:
    manifest = {"dr": True, "evt": [{"reason": "BackOff"}], "z_cpu": "4.2", "z_mem": "1.0"}
    fp1 = autonomous_decider._fingerprint(manifest)
    fp2 = autonomous_decider._fingerprint(dict(manifest))
    assert fp1 == fp2
    assert autonomous_decider._parse_csv_set("a, b,,c") == {"a", "b", "c"}

    payload = autonomous_decider._parse_tool_payload(
        '```json\n{"tool":"echo","args":{"msg":"hi"}}\n```'
    )
    assert payload.tool == "echo"
    clear = autonomous_decider._parse_react_turn(
        json.dumps({"thought": "ok", "reasoning_path": "p", "action": "CLEAR"})
    )
    assert clear == ("ok", "p", True, None)
    tool = autonomous_decider._parse_react_turn(
        json.dumps({"thought": "use echo", "action": {"tool": "echo", "args": {"msg": "x"}}})
    )
    assert tool is not None
    assert tool[3] == {"tool": "echo", "args": {"msg": "x"}}
    assert autonomous_decider._parse_react_turn("not-json") is None
    assert autonomous_decider._is_clear("CLEAR no action")
    assert "CPU" in autonomous_decider._sigma_hint(manifest)
    prompt = autonomous_decider._build_user_prompt(manifest, True, manifest["evt"])
    assert "Manifest:" in prompt
    system = autonomous_decider._system_prompt({"echo"}, {"multi-agent"})
    assert "Allowed tools: echo" in system
    react_system = autonomous_decider._system_prompt_react({"echo"}, {"multi-agent"}, "schema")
    assert "schema" in react_system
    assert autonomous_decider._react_state_key("abc").endswith("abc")
    assert autonomous_decider._args_fingerprint({"b": 2, "a": 1}) == autonomous_decider._args_fingerprint(
        {"a": 1, "b": 2}
    )


def _contrast_evidence() -> dict[str, dict[str, object]]:
    labels = {
        "namespace": "multi-agent",
        "deployment": "api",
        "pod": "api-abc",
        "container": "api",
        "alertname": "HighCPU",
    }
    annotations = {"summary": "CPU high", "description": "Synthetic alert text"}
    return {
        "alert": {
            "symptom_group": "workload_resource",
            "alert_hint": "High CPU on workload",
            "alert_rule": "sum(rate(container_cpu_usage_seconds_total[5m]))",
            "canonical_query_snippet": json.dumps(
                {"labels": labels, "annotations": annotations}
            ),
        },
        "k8s_clinical_pod_status": {
            "result": "PASSED",
            "extracted_fact": json.dumps({"pods": [{"phase": "Running"}]}),
        },
        "k8s_clinical_pod_metrics": {
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {
                    "containers": [
                        {"name": "api", "cpu": "0n", "memory": "1Mi"},
                    ]
                }
            ),
        },
        "prom_pod_cpu_cores": {
            "result": "PASSED",
            "raw": "cpu=2",
            "extracted_fact": json.dumps({"s0": 2, "unit": "cores"}),
        },
    }


def test_alert_sdk_truth_compare_detects_cpu_contrast() -> None:
    evidence = _contrast_evidence()
    contrast = truth.compare_alert_claim_to_sdk_state(evidence)
    assert contrast is not None
    assert "không đáng kể" in contrast
    body = truth.build_contrast_operator_telegram_body(evidence, contrast, "trace-1", locale="en")
    assert "STATE_MACHINE_CONTRAST" in body
    assert "multi-agent" in body
    diagnosis = truth.build_contrast_diagnosis_for_action(evidence, contrast, max_len=180)
    assert diagnosis.endswith("…")


def test_alert_sdk_truth_compare_guard_paths() -> None:
    assert truth.compare_alert_claim_to_sdk_state({}) is None
    evidence = _contrast_evidence()
    status = evidence["k8s_clinical_pod_status"]
    status["extracted_fact"] = json.dumps({"phase": "Pending"})
    assert truth.compare_alert_claim_to_sdk_state(evidence) is None
    assert truth._cpu_usage_effectively_zero("5m")
    assert not truth._cpu_usage_effectively_zero("500m")
    assert truth._memory_usage_low_for_mem_alert("1Mi", True)
    assert not truth._memory_usage_low_for_mem_alert("1Gi", True)


def test_vm_slot_accumulation_full_pod_flow() -> None:
    slots = vm_slots.extract_vm_slots_from_text("cpu pod nginx-abc-123 namespace multi-agent 30m")
    assert slots["intent"] == "cpu"
    assert slots["duration"] == "30m"
    enriched = vm_slots.enrich_slots_from_discovery(
        {"intent": "ram", "pod_name": "nginx"},
        [{"name": "nginx-abc-123", "namespace": "multi-agent"}],
    )
    assert enriched["namespace"] == "multi-agent"
    merged = vm_slots.merge_vm_slots(enriched, "disk 2h")
    assert merged["intent"] == "disk"
    assert vm_slots.vm_slots_ready(merged)
    args = vm_slots.vm_slots_to_tool_args(merged, None)
    assert args["target_type"] == "pod"
    assert args["duration"] == "2h"


def test_vm_slot_accumulation_host_flow_and_nudges() -> None:
    assert vm_slots.followup_indicates_host("check worker node")
    host_slots = {"target_type": "host", "intent": "cpu", "duration": "6h"}
    assert vm_slots.vm_slots_ready(host_slots)
    assert vm_slots.vm_slots_to_tool_args(host_slots, None)["target_type"] == "host"
    assert "Host" in vm_slots.nudge_vm_slots_message(host_slots)
    pod_msg = vm_slots.nudge_vm_slots_message({"namespace": "multi-agent"})
    assert "pod/workload" in pod_msg
    with pytest.raises(ValueError):
        vm_slots.vm_slots_to_tool_args({"target_type": "host"}, None)
    with pytest.raises(ValueError):
        vm_slots.vm_slots_to_tool_args({"intent": "cpu"}, None)


def test_promql_presets_host_and_pod_queries() -> None:
    q, note, meta = promql_presets.build_dynamic_promql("host", "disk_io", node="node-1")
    assert "node_disk_read_bytes_total" in q
    assert meta["used_profile"] == "node_exporter_disk_read"
    q2, note2, meta2 = promql_presets.build_dynamic_promql(
        "pod",
        "memory",
        namespace='multi"agent',
        workload_prefix="api",
    )
    assert "container_memory_working_set_bytes" in q2
    assert '\\"' in q2
    assert meta2["used_profile"] == "cAdvisor_workload_regex"
    assert "workload" in note2
    with pytest.raises(ValueError):
        promql_presets.build_dynamic_promql("pod", "cpu", namespace="")


def test_promql_presets_kube_state_queries_and_intents() -> None:
    q, _note, meta = promql_presets.build_kube_state_promql(
        "replicas_available",
        namespace="multi-agent",
        deployment="api",
    )
    assert "kube_deployment_status_replicas_available" in q
    assert meta["used_profile"] == "kube_state_replicas_available"
    q2, _note2, meta2 = promql_presets.build_kube_state_promql("pods", namespace="multi-agent")
    assert 'phase="Running"' in q2
    assert meta2["used_profile"] == "kube_state_pods_running"
    assert promql_presets.resolve_intent_from_keywords("băng thông mạng") == "network"
    assert promql_presets.resolve_intent_from_keywords("ghi đĩa") == "disk"
    with pytest.raises(ValueError):
        promql_presets.build_kube_state_promql("replicas", namespace="multi-agent")


def test_fast_path_auto_execute_allowlist_with_shell_enabled() -> None:
    from unittest.mock import patch
    from workers.routing_policy import fast_path_auto_execute_allowlist, GOD_MODE_FAST_PATH_EXTRA_TOOLS
    with patch("workers.routing_policy.shell_fast_path_enabled", return_value=True):
        result = fast_path_auto_execute_allowlist(None)
    assert result.issuperset(GOD_MODE_FAST_PATH_EXTRA_TOOLS)
