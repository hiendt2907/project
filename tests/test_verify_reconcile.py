"""Behavioral tests for VERIFY ground-truth reconciliation.

These assert the system can REFUTE a wrong advisory, not just stamp it.
No tautologies: each test feeds a claim + a live pod state and checks the
verdict flips to match reality.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from workers.verify_reconcile import (
    PodGroundTruth,
    ReconcileOutcome,
    cap_assessments,
    detect_claim_signals,
    reconcile_advisory,
    reconcile_signal,
)


def _healthy() -> PodGroundTruth:
    return PodGroundTruth(found=True, pod_name="nginx-test-1", phase="Running", restarts=0, ready=True)


def _oomed() -> PodGroundTruth:
    return PodGroundTruth(
        found=True, pod_name="nginx-test-1", phase="Running", restarts=3, ready=False,
        terminated_reasons=frozenset({"OOMKilled"}),
    )


class TestClaimSignals:
    def test_oom_claim_detected(self) -> None:
        sigs = detect_claim_signals("Pod nginx-test is OOMKilled due to memory working set exceeding limit")
        assert "oom" in sigs

    def test_crash_claim_detected(self) -> None:
        assert "crash" in detect_claim_signals("container keeps restarting in CrashLoopBackOff")

    def test_no_signal_for_generic_text(self) -> None:
        assert detect_claim_signals("network latency between services is elevated") == ()


class TestReconcileSignal:
    def test_oom_claim_on_healthy_pod_is_refuted(self) -> None:
        verdict, why = reconcile_signal("oom", _healthy())
        assert verdict == "refuted"
        assert "healthy" in why.lower() or "contradict" in why.lower()

    def test_oom_claim_on_oomed_pod_is_confirmed(self) -> None:
        verdict, _ = reconcile_signal("oom", _oomed())
        assert verdict == "confirmed"

    def test_claim_on_missing_pod_is_refuted(self) -> None:
        verdict, why = reconcile_signal("oom", PodGroundTruth(found=False, pod_name="ghost"))
        assert verdict == "refuted"
        assert "not found" in why.lower()

    def test_crash_claim_on_healthy_pod_refuted(self) -> None:
        assert reconcile_signal("crash", _healthy())[0] == "refuted"

    def test_crash_claim_on_restarting_pod_confirmed(self) -> None:
        assert reconcile_signal("crash", _oomed())[0] == "confirmed"

    def test_notready_on_ready_pod_refuted(self) -> None:
        assert reconcile_signal("notready", _healthy())[0] == "refuted"


class TestCapAssessments:
    def test_refuted_ground_truth_caps_confirmed_to_refuted(self) -> None:
        out = cap_assessments([{"kb_id": "k1", "verdict": "confirmed"}], "refuted")
        assert out[0]["verdict"] == "refuted"
        assert "capped" in out[0]["reason"]

    def test_unverifiable_ground_truth_downgrades_confirmed(self) -> None:
        out = cap_assessments([{"kb_id": "k1", "verdict": "confirmed"}], "unverifiable")
        assert out[0]["verdict"] == "unverifiable"

    def test_confirmed_ground_truth_keeps_llm_verdict(self) -> None:
        out = cap_assessments([{"kb_id": "k1", "verdict": "confirmed"}], "confirmed")
        assert out[0]["verdict"] == "confirmed"

    def test_never_upgrades_refuted(self) -> None:
        out = cap_assessments([{"kb_id": "k1", "verdict": "refuted"}], "confirmed")
        # confirmed ground truth keeps LLM verdict — which was refuted; never invented up
        assert out[0]["verdict"] == "refuted"

    def test_input_not_mutated(self) -> None:
        src = [{"kb_id": "k1", "verdict": "confirmed"}]
        cap_assessments(src, "refuted")
        assert src[0]["verdict"] == "confirmed"  # original untouched (immutability)


class TestReconcileAdvisoryE2E:
    @pytest.mark.asyncio
    async def test_hallucinated_oom_on_healthy_pod_refuted(self, monkeypatch) -> None:
        """The exact live bug: advisory claims OOM, pod is Running 0-restart → refuted."""
        async def fake_read(ctx, ns, hint):
            return _healthy()

        monkeypatch.setattr("workers.verify_reconcile.read_pod_ground_truth", fake_read)
        advisory = SimpleNamespace(
            root_cause="Pod nginx-test in namespace multi-agent is OOMKilled, working set exceeds limit",
            affected_workload="multi-agent/nginx-test",
        )
        outcome = await reconcile_advisory(SimpleNamespace(), advisory)
        assert isinstance(outcome, ReconcileOutcome)
        assert outcome.verdict == "refuted"

    @pytest.mark.asyncio
    async def test_real_oom_confirmed(self, monkeypatch) -> None:
        async def fake_read(ctx, ns, hint):
            return _oomed()

        monkeypatch.setattr("workers.verify_reconcile.read_pod_ground_truth", fake_read)
        advisory = SimpleNamespace(
            root_cause="Pod nginx-test OOMKilled by kubelet memory limit",
            affected_workload="multi-agent/nginx-test",
        )
        outcome = await reconcile_advisory(SimpleNamespace(), advisory)
        assert outcome.verdict == "confirmed"

    @pytest.mark.asyncio
    async def test_no_signal_is_unverifiable(self) -> None:
        advisory = SimpleNamespace(
            root_cause="Network latency elevated between frontend and backend",
            affected_workload="multi-agent/frontend",
        )
        outcome = await reconcile_advisory(SimpleNamespace(), advisory)
        assert outcome.verdict == "unverifiable"
