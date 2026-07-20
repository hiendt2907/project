"""TDD — grounding gate cho lane advisory (INV_DIAG_GROUNDED áp sang advisory path).

Bối cảnh: trace gw-prom-84cd18edddb2 (2026-07-15) — alert OmniBaselineMemZHigh
(self-monitoring, evidence chỉ có node_cpu_saturation PASSED) nhưng qwen2.5-coder:7b
trả root_cause "Pod nginx-test bị OOMKilled do vượt giới hạn bộ nhớ cgroup" — copy
nguyên văn ví dụ trong system prompt, trace_id để nguyên placeholder
"<copy from input>", và advisory bịa này đi hết pipeline tới Telegram + CRAT.

Gate phải: phát hiện claim không neo được vào evidence_text (workload/path/%/
placeholder), hạ verdict về INVESTIGATE, confidence=low, xoá remediation, lọc
verification_steps nhiễm template, cap forecast severity.
"""

from __future__ import annotations

from pkg.reasoning.analyst_advisory_schema import (
    AnalystAdvisory,
    confidence_to_float,
)
from workers.advisory_grounding_gate import (
    apply_advisory_grounding_gate,
    collect_ungrounded_claims,
)


def _advisory(
    *,
    root_cause: str,
    affected_workload: str = "unknown",
    verdict: str = "URGENT",
    confidence: str = "high",
    verification_steps: list[dict] | None = None,
    proposed_remediation: list[dict] | None = None,
) -> AnalystAdvisory:
    return AnalystAdvisory(
        trace_id="t-1",
        verdict=verdict,
        root_cause=root_cause,
        confidence=confidence,
        affected_workload=affected_workload,
        verification_steps=verification_steps
        or [
            {
                "order": 1,
                "layer": "kubernetes",
                "command": "kubectl get events -n multi-agent",
                "expected_output": "no warnings",
                "rationale": "check recent events",
            }
        ],
        proposed_remediation=proposed_remediation
        or [
            {
                "order": 1,
                "action": "Tăng giới hạn bộ nhớ cho pod",
                "args": {},
                "approval_required": True,
                "rollback_plan": "revert",
            }
        ],
        forecast={
            "method": "heuristic",
            "basis": "test",
            "forecasts": [
                {
                    "timeframe": "6h",
                    "severity": "catastrophic",
                    "prediction": "full outage",
                    "confidence": "high",
                }
            ],
            "note": "",
        },
    )


EVIDENCE_META_SELF = (
    "[ALERT_CONTEXT]\n  rule: IngressPrometheus\n"
    "  error_hint: OmniBaselineMemZHigh abs(omni:mem:z) > 3\n"
    "[EVIDENCE]\n  probe: node_cpu_saturation\n  status: PASSED\n"
    "  metrics_or_facts: {\"s0\": 0.064}\n"
)

EVIDENCE_REAL_OOM = (
    "[ALERT_CONTEXT]\n  alertname: KubePodCrashLooping namespace=shop pod=cart-api-7f9\n"
    "[EVIDENCE]\n  probe: k8s_clinical_pod_status\n  status: FAILED\n"
    "  error_or_raw: pod cart-api-7f9 OOMKilled restartCount=7 limits.memory=512Mi\n"
    "  workload: shop/cart-api\n"
)


# --------------------------------------------------------------------------- #
# collect_ungrounded_claims                                                    #
# --------------------------------------------------------------------------- #


def test_parroted_workload_name_is_ungrounded():
    claims = collect_ungrounded_claims(
        "Pod nginx-test bị OOMKilled do vượt giới hạn bộ nhớ cgroup",
        EVIDENCE_META_SELF,
    )
    assert "nginx-test" in claims


def test_placeholder_tokens_are_always_ungrounded():
    claims = collect_ungrounded_claims("<copy from input>", EVIDENCE_META_SELF)
    assert "<copy from input>" in claims


def test_grounded_workload_name_passes():
    claims = collect_ungrounded_claims(
        "Pod cart-api-7f9 bị OOMKilled do vượt limits.memory 512Mi",
        EVIDENCE_REAL_OOM,
    )
    assert claims == []


def test_grounding_check_is_case_insensitive():
    # Regression benchmark case_009: evidence viết "Ollama unreachable" (hoa),
    # model viết workload/claim thường "ollama" → gate cũ so case-sensitive
    # và fire nhầm trên advisory hoàn toàn có căn cứ.
    evidence = (
        "[ALERT_CONTEXT] alertname: OllamaDown\n"
        "[EVIDENCE] probe: llm_health status: FAILED\n"
        "  error_or_raw: Ollama unreachable at host.orb.internal:11434, "
        "Deployment multi-agent/Omni-Fullstack degraded\n"
    )
    claims = collect_ungrounded_claims(
        "Service ollama không phản hồi khiến deployment omni-fullstack treo", evidence
    )
    assert claims == []
    adv = _advisory(
        root_cause="Service ollama không phản hồi khiến deployment omni-fullstack treo",
        affected_workload="multi-agent/omni-fullstack",
    )
    gated, ungrounded = apply_advisory_grounding_gate(adv, evidence)
    assert ungrounded == []
    assert gated.verdict == adv.verdict


def test_ungrounded_path_and_percentage_detected():
    claims = collect_ungrounded_claims(
        "Phân vùng /var/data đầy 97% gây eviction", EVIDENCE_META_SELF
    )
    assert "/var/data" in claims
    assert "97%" in claims


def test_english_compound_prose_is_not_a_claim():
    # Dash-name chỉ là claim khi được khẳng định là OBJECT (sau keyword loại K8s
    # hoặc trong cặp ns/name) — prose tiếng Anh nhiều từ ghép gạch nối vô hại.
    for prose in (
        "Cảnh báo read-only self-monitoring theo kill-chain và consumer-group",
        "Out-of-memory errors tăng nhưng issue đã self-resolved",
        "Rate limiting triggered (429 responses) and self-resolved naturally",
    ):
        assert collect_ungrounded_claims(prose, EVIDENCE_META_SELF) == [], prose


def test_ns_slash_name_pair_is_a_claim():
    claims = collect_ungrounded_claims(
        "Workload multi-agent/target-workload gặp sự cố", EVIDENCE_META_SELF
    )
    assert "target-workload" in claims
    assert "multi-agent" in claims


# --------------------------------------------------------------------------- #
# apply_advisory_grounding_gate                                                #
# --------------------------------------------------------------------------- #


def test_gate_neutralizes_parroted_advisory():
    adv = _advisory(
        root_cause="Pod nginx-test bị OOMKilled do vượt giới hạn bộ nhớ cgroup",
        affected_workload="default/nginx-test",
        verdict="URGENT",
        confidence="medium",
    )
    gated, ungrounded = apply_advisory_grounding_gate(adv, EVIDENCE_META_SELF)

    assert ungrounded, "gate phải phát hiện claim bịa"
    assert gated.verdict == "INVESTIGATE"
    assert gated.confidence == "low"
    assert gated.affected_workload == "unknown"
    assert gated.root_cause.startswith("[UNGROUNDED")
    assert gated.proposed_remediation == []
    # original object must not be mutated (immutability)
    assert adv.verdict == "URGENT"
    assert adv.affected_workload == "default/nginx-test"


def test_gate_caps_forecast_severity_when_fired():
    adv = _advisory(
        root_cause="Pod nginx-test bị OOMKilled",
        affected_workload="default/nginx-test",
    )
    gated, _ = apply_advisory_grounding_gate(adv, EVIDENCE_META_SELF)
    assert all(f.severity in ("healthy", "degraded") for f in gated.forecast.forecasts)
    assert all(f.confidence == "low" for f in gated.forecast.forecasts)
    assert "grounding" in gated.forecast.note.lower()


def test_gate_drops_only_contaminated_verification_steps():
    adv = _advisory(
        root_cause="Pod nginx-test bị OOMKilled",
        affected_workload="default/nginx-test",
        verification_steps=[
            {
                "order": 1,
                "layer": "kubernetes",
                "command": "kubectl describe pod nginx-test -n default",
                "expected_output": "OOMKilled",
                "rationale": "xác minh workload",
            },
            {
                "order": 2,
                "layer": "os_baremetal",
                "command": "top -b -n1",
                "expected_output": "load < 4.0",
                "rationale": "kiểm tra host",
            },
        ],
    )
    gated, _ = apply_advisory_grounding_gate(adv, EVIDENCE_META_SELF)
    commands = [s.command for s in gated.verification_steps]
    assert "top -b -n1" in commands
    assert not any("nginx-test" in c for c in commands)


def test_gate_passes_grounded_advisory_unchanged():
    adv = _advisory(
        root_cause="Pod cart-api-7f9 bị OOMKilled do vượt limits.memory 512Mi",
        affected_workload="shop/cart-api",
        verdict="URGENT",
        confidence="high",
        verification_steps=[
            {
                "order": 1,
                "layer": "kubernetes",
                "command": "kubectl describe pod cart-api-7f9 -n shop",
                "expected_output": "OOMKilled",
                "rationale": "xác minh restartCount",
            }
        ],
    )
    gated, ungrounded = apply_advisory_grounding_gate(adv, EVIDENCE_REAL_OOM)
    assert ungrounded == []
    assert gated.verdict == "URGENT"
    assert gated.confidence == "high"
    assert gated.root_cause == adv.root_cause
    assert len(gated.proposed_remediation) == 1


def test_gate_flags_unfilled_placeholder_in_workload():
    adv = _advisory(
        root_cause="Thiếu bằng chứng cụ thể",
        affected_workload="<ns>/<dep>",
    )
    gated, ungrounded = apply_advisory_grounding_gate(adv, EVIDENCE_META_SELF)
    assert ungrounded
    assert gated.affected_workload == "unknown"


def test_gate_ignores_unknown_workload():
    adv = _advisory(
        root_cause="Không đủ bằng chứng cho OmniBaselineMemZHigh",
        affected_workload="unknown",
        verdict="INVESTIGATE",
        confidence="low",
    )
    gated, ungrounded = apply_advisory_grounding_gate(adv, EVIDENCE_META_SELF)
    assert ungrounded == []
    assert gated.verdict == "INVESTIGATE"


# --------------------------------------------------------------------------- #
# confidence_to_float — SUGGEST_REMEDIATION phải phản ánh confidence thật      #
# --------------------------------------------------------------------------- #


def test_confidence_to_float_honest_mapping():
    assert confidence_to_float("high") == 0.9
    assert confidence_to_float("medium") == 0.6
    assert confidence_to_float("low") == 0.3


def test_confidence_to_float_defaults_low_on_garbage():
    assert confidence_to_float("") == 0.3
    assert confidence_to_float("HIGH ") == 0.9
    assert confidence_to_float("banana") == 0.3
