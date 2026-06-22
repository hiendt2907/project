"""Post-mutate SDK verify — optional probe matrix (INCONCLUSIVE vs FAILED)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workers.diagnostic_evidence import ProbeRunRaw
from workers.post_mutate_sdk_verify import optional_probe_ids_from_ctx, run_verify_probes
from workers.proactive_models import AnomalyEvent


def _ev() -> AnomalyEvent:
    return AnomalyEvent.model_validate(
        {
            "trace_id": "t-verify",
            "canonical_query": "{}",
        }
    )


def test_optional_probe_ids_from_ctx_default():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_sdk_verify_optional_probes="prom_pod_cpu_cores,prom_pod_memory_wss")
    assert "prom_pod_cpu_cores" in optional_probe_ids_from_ctx(ctx)


@pytest.mark.asyncio
async def test_run_verify_optional_inconclusive_ok():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_sdk_verify_optional_probes="prom_pod_cpu_cores")

    async def fake_run(pid: str, _ctx, _ev):
        if pid == "k8s_clinical_pod_status":
            return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text="ok")
        if pid == "prom_pod_cpu_cores":
            return ProbeRunRaw(probe_name=pid, status="INCONCLUSIVE", raw_text="no data")
        return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text="")

    with patch("workers.post_mutate_sdk_verify.run_probe", side_effect=fake_run):
        ok, summary, raws = await run_verify_probes(
            ctx,
            trace="t1",
            probe_ids=["k8s_clinical_pod_status", "prom_pod_cpu_cores"],
            ev=_ev(),
        )
    assert ok is True
    assert "INCONCLUSIVE" in summary


@pytest.mark.asyncio
async def test_run_verify_optional_failed_still_fails():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_sdk_verify_optional_probes="prom_pod_cpu_cores")

    async def fake_run(pid: str, _ctx, _ev):
        if pid == "k8s_clinical_pod_status":
            return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text="ok")
        if pid == "prom_pod_cpu_cores":
            return ProbeRunRaw(probe_name=pid, status="FAILED", raw_text="threshold")
        return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text="")

    with patch("workers.post_mutate_sdk_verify.run_probe", side_effect=fake_run):
        ok, _, _ = await run_verify_probes(
            ctx,
            trace="t2",
            probe_ids=["k8s_clinical_pod_status", "prom_pod_cpu_cores"],
            ev=_ev(),
        )
    assert ok is False


@pytest.mark.asyncio
async def test_run_verify_skipped_counts_as_pass():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_sdk_verify_optional_probes="")

    async def fake_run(pid: str, _ctx, _ev):
        if pid == "k8s_clinical_pod_log_previous":
            return ProbeRunRaw(probe_name=pid, status="SKIPPED", raw_text="healthy")
        return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text="ok")

    with patch("workers.post_mutate_sdk_verify.run_probe", side_effect=fake_run):
        ok, _, _ = await run_verify_probes(
            ctx,
            trace="t-skip",
            probe_ids=["k8s_clinical_pod_status", "k8s_clinical_pod_log_previous"],
            ev=_ev(),
        )
    assert ok is True


@pytest.mark.asyncio
async def test_run_verify_required_inconclusive_fails():
    ctx = MagicMock()
    ctx.settings = MagicMock(omni_sdk_verify_optional_probes="prom_pod_cpu_cores")

    async def fake_run(pid: str, _ctx, _ev):
        if pid == "k8s_clinical_pod_status":
            return ProbeRunRaw(probe_name=pid, status="INCONCLUSIVE", raw_text="?")
        return ProbeRunRaw(probe_name=pid, status="PASSED", raw_text="")

    with patch("workers.post_mutate_sdk_verify.run_probe", side_effect=fake_run):
        ok, _, _ = await run_verify_probes(
            ctx,
            trace="t3",
            probe_ids=["k8s_clinical_pod_status"],
            ev=_ev(),
        )
    assert ok is False
