"""Smart tier-1/tier-2 diagnostic dispatcher — expected probe registration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.diagnostic_evidence import ProbeRunRaw
from workers.diagnostic_dispatcher import run_diagnostic_pipeline
from workers.proactive_models import AnomalyEvent


@pytest.mark.asyncio
async def test_pod_container_state_registers_smart_plan_probes() -> None:
    cq = json.dumps(
        {
            "labels": {
                "namespace": "multi-agent",
                "pod": "nginx-test-abc",
                "reason": "CreateContainerError",
            },
            "annotations": {},
        }
    )
    ev = AnomalyEvent(
        trace_id="trace-smart-1",
        canonical_query=cq,
        error_hint="pod unhealthy",
        namespace="multi-agent",
    )
    ctx = MagicMock()
    ws = MagicMock()
    ws.diagnostic_dictionary_enabled = True
    ws.diagnostic_matrix_path = "/dev/null/matrix.yaml"
    ws.kafka_topic_diagnostic_evidence = "omni-evidence"
    ctx.settings = ws
    ctx.kafka = MagicMock()
    ctx.kafka.send_dict = AsyncMock()

    status_hint = {
        "source": "K8s_SDK",
        "kind": "PodStatus",
        "phase": "Pending",
        "ready_false": True,
        "waiting_reasons": ["CreateContainerError"],
        "has_crash_loop": False,
        "namespace": "multi-agent",
        "pod": "nginx-test-abc",
    }
    status_raw = ProbeRunRaw(
        probe_name="k8s_clinical_pod_status",
        status="PASSED",
        raw_text="phase=Pending",
        structured_hint=status_hint,
    )

    async def _fake_run_probe(pid: str, _ctx: object, _ev: object) -> ProbeRunRaw:
        if pid == "k8s_clinical_pod_status":
            return status_raw
        return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text=f"ok:{pid}")

    captured: list[list[str]] = []

    async def _capture_expected(_redis: object, trace: str, probes: list[str]) -> None:
        captured.append(list(probes))

    with (
        patch("workers.diagnostic_dispatcher.is_workload_resource_alert", return_value=False),
        patch("workers.diagnostic_dispatcher.is_kube_pod_container_state_alert", return_value=True),
        patch("workers.diagnostic_dispatcher.run_probe", side_effect=_fake_run_probe),
        patch("workers.diagnostic_dispatcher.register_diag_expected_probes", side_effect=_capture_expected),
    ):
        await run_diagnostic_pipeline(ctx, ev)

    assert captured, "register_diag_expected_probes should be called"
    assert captured[0] == [
        "k8s_clinical_pod_status",
        "k8s_clinical_pod_events",
        "k8s_resource_quota_probe",
    ]
    assert ctx.kafka.send_dict.await_count >= 3
