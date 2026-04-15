"""
tests/test_omni_stateful_loop.py

Golden test suite for the Omni Stateful Closed-Loop (Sprint 7).

Coverage targets:
  - RemediationContext and to_prompt_block() (llm_contract.py)
  - phase4_execute() loop logic (mvp_api.py)
  - phase5_verify() owner-resolution + health-check paths (mvp_api.py)
  - get_resource_owner() traversal (k8s_cluster_tools.py)
  - OLLAMA_BASE_URL auto-detection (mvp_api.py)
  - VERIFY_BACKOFF_SECONDS backoff (mvp_api.py)

Run with coverage:
    pytest --cov=src --cov=scripts tests/ --cov-report=term-missing
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Imports — conftest.py already added src/ and scripts/ to sys.path.
# ---------------------------------------------------------------------------
import mvp_api
from mvp_api import (
    AlertInput,
    ExecutionResponse,
    Lane,
    LanedAlert,
    phase4_execute,
    phase5_verify,
)
from pkg.autonomy.llm_contract import (
    ActionRecord,
    HighLevelRemediationPlan,
    ObservationRecord,
    OutcomeRecord,
    RemediationContext,
)
from workers.k8s_cluster_tools import get_resource_owner


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def cm_fault_alert() -> AlertInput:
    """Alert representing a CreateContainerConfigError due to a missing ConfigMap."""
    return AlertInput(
        alertname="KubePodCreateContainerConfigError",
        namespace="lab-test",
        pod="nginx-7d9f8b6c4-xk2pq",
        container="nginx",
        severity="critical",
        message="failed to create container: missing ConfigMap nginx-config",
    )


@pytest.fixture
def oom_alert() -> AlertInput:
    return AlertInput(
        alertname="KubePodOOMKilled",
        namespace="multi-agent",
        pod="api-server-7d9f8b-xk2",
        container="api-server",
        severity="critical",
    )


@pytest.fixture
def app_log_alert() -> AlertInput:
    return AlertInput(
        alertname="HttpErrorRate5xx",
        namespace="production",
        pod="api-server-7d9f8b-xk2",
        container="api-server",
        severity="critical",
        message="sustained 503 errors on /checkout",
    )


@pytest.fixture
def plan_patch_cm() -> HighLevelRemediationPlan:
    return HighLevelRemediationPlan(
        action="patch_configmap_key",
        target_ref="nginx-config",
        namespace="lab-test",
        configmap_key="placeholder",
        configmap_value="omni-auto-created",
        reasoning=(
            "CreateContainerConfigError: ConfigMap nginx-config does not exist. "
            "Creating it with a placeholder key to unblock container startup."
        ),
    )


@pytest.fixture
def plan_rollout_restart() -> HighLevelRemediationPlan:
    return HighLevelRemediationPlan(
        action="rollout_restart",
        target_ref="nginx",
        namespace="lab-test",
        reasoning=(
            "ConfigMap nginx-config was created in the previous iteration but the "
            "Deployment still has 0 available replicas. The existing pod predates "
            "the ConfigMap and will not pick it up automatically. A rollout_restart "
            "forces a new pod that finds the ConfigMap on startup."
        ),
    )


# ===========================================================================
# Scenario 1 — 2-Iteration Recovery (ConfigMap Missing → rollout_restart)
# ===========================================================================

class TestTwoIterationRecovery:
    """
    Validates the core closed-loop reasoning chain:
      Iteration 1: LLM creates the missing ConfigMap.
      Iteration 2: LLM sees UNHEALTHY outcome in history → picks rollout_restart.
      converged=True after iteration 2 verify returns healthy.
    """

    async def test_convergence_on_iteration_2(
        self, cm_fault_alert, plan_patch_cm, plan_rollout_restart
    ):
        """Full 2-iteration loop converges with correct state transitions."""
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            # LLM returns different plans on each call
            patch(
                "mvp_api.phase3_output",
                new_callable=AsyncMock,
                side_effect=[plan_patch_cm, plan_rollout_restart],
            ) as mock_llm,
            # Execution always succeeds
            patch(
                "mvp_api._execute_library_tool",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_exec,
            # Verify: iter1 unhealthy, iter2 healthy
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                side_effect=[
                    (False, "Deployment nginx/lab-test: desired=1 available=0 ready=0"),
                    (True, "Deployment nginx/lab-test: desired=1 available=1 ready=1"),
                ],
            ),
            # Skip actual backoff sleep in tests
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
        ):
            resp = await phase4_execute(cm_fault_alert)

        assert resp.iterations == 2, "should converge on iteration 2"
        assert resp.converged is True
        assert resp.executed is True
        assert resp.plan.action == "rollout_restart"
        assert mock_llm.call_count == 2
        assert mock_exec.call_count == 2

    async def test_history_injected_in_second_llm_call(
        self, cm_fault_alert, plan_patch_cm, plan_rollout_restart
    ):
        """
        RemediationContext history must appear in the system prompt on iteration 2.

        This test validates the core stateful-loop mechanism: the LLM receives
        what was tried, the observed outcome, and guidelines for next steps.
        """
        captured_prompts: list[tuple[str, str]] = []

        async def _capture_llm(system_prompt: str, user_message: str) -> HighLevelRemediationPlan:
            captured_prompts.append((system_prompt, user_message))
            return plan_patch_cm if len(captured_prompts) == 1 else plan_rollout_restart

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api.phase3_output", side_effect=_capture_llm),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                side_effect=[
                    (False, "Deployment nginx/lab-test: desired=1 available=0 ready=0"),
                    (True, "Deployment nginx/lab-test: desired=1 available=1 ready=1"),
                ],
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
        ):
            resp = await phase4_execute(cm_fault_alert)

        assert resp.converged is True
        assert len(captured_prompts) == 2

        # Iteration 1: no history block
        iter1_prompt = captured_prompts[0][0]
        assert "REMEDIATION HISTORY" not in iter1_prompt

        # Iteration 2: history block present with key signals
        iter2_prompt = captured_prompts[1][0]
        assert "REMEDIATION HISTORY" in iter2_prompt
        assert "patch_configmap_key" in iter2_prompt
        assert "UNHEALTHY" in iter2_prompt
        assert "END HISTORY" in iter2_prompt

    async def test_shadow_writeback_called_on_convergence(
        self, cm_fault_alert, plan_patch_cm, plan_rollout_restart
    ):
        """_shadow_writeback is called exactly once, on the converging iteration."""
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch(
                "mvp_api.phase3_output",
                new_callable=AsyncMock,
                side_effect=[plan_patch_cm, plan_rollout_restart],
            ),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                side_effect=[
                    (False, "desired=1 available=0 ready=0"),
                    (True, "desired=1 available=1 ready=1"),
                ],
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock) as mock_shadow,
        ):
            await phase4_execute(cm_fault_alert)

        mock_shadow.assert_called_once()

    async def test_response_fields(self, cm_fault_alert, plan_patch_cm, plan_rollout_restart):
        """ExecutionResponse carries correct lane, iterations, and converged fields."""
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch(
                "mvp_api.phase3_output",
                new_callable=AsyncMock,
                side_effect=[plan_patch_cm, plan_rollout_restart],
            ),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                side_effect=[
                    (False, "desired=1 available=0 ready=0"),
                    (True, "desired=1 available=1 ready=1"),
                ],
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
        ):
            resp = await phase4_execute(cm_fault_alert)

        assert resp.lane == "state"
        assert resp.trace_id  # non-empty UUID
        assert resp.iterations == 2
        assert resp.converged is True
        assert resp.resolution_state == "converged"
        assert resp.executed is True


# ===========================================================================
# Scenario 2 — Security Gate: INV_NAMESPACE_ISOLATION
# ===========================================================================

class TestSecurityGate:
    """Invariant gate blocks mutations on non-allowed namespaces — loop terminates."""

    async def test_inv_namespace_isolation_blocks_and_terminates(self, oom_alert):
        """
        When INV_NAMESPACE_ISOLATION fires, the loop breaks immediately.
        The response carries a noop plan with the invariant code in the reasoning.
        """
        forbidden_plan = HighLevelRemediationPlan(
            action="rollout_restart",
            target_ref="api-server",
            namespace="kube-system",  # not in allowed set
        )

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=forbidden_plan),
            patch(
                "mvp_api._apply_invariants",
                return_value=(False, "INV_NAMESPACE_ISOLATION"),
            ),
            patch(
                "mvp_api._execute_library_tool",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            resp = await phase4_execute(oom_alert)

        assert resp.plan.action == "noop"
        assert "INV_NAMESPACE_ISOLATION" in resp.plan.reasoning
        assert resp.converged is False
        assert resp.iterations == 1
        mock_exec.assert_not_called()

    async def test_invariant_block_does_not_call_execute(self, oom_alert):
        """Execution must never be called when an invariant blocks the plan."""
        blocked_plan = HighLevelRemediationPlan(
            action="patch_deployment_resource",
            target_ref="api-server",
            namespace="default",
            patch_json='{"spec":{"template":{"spec":{"containers":[{"name":"api","resources":{"limits":{"memory":"1Gi"}}}]}}}}',
        )

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=blocked_plan),
            patch("mvp_api._apply_invariants", return_value=(False, "INV_READ_BEFORE_MUTATE")),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock) as mock_exec,
        ):
            resp = await phase4_execute(oom_alert)

        mock_exec.assert_not_called()
        assert resp.executed is False


# ===========================================================================
# Scenario 3 — Fail-Closed Loki (APP_LOG lane)
# ===========================================================================

class TestFailClosedLoki:
    """
    When Loki is unavailable, the APP_LOG lane must return noop immediately
    without calling the LLM (fail-closed: no mutation without log evidence).
    """

    async def test_loki_unavailable_returns_noop_without_llm_call(self, app_log_alert):
        """LLM is never called when LOKI_BASE_URL is empty (fail-closed)."""
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("app_log", "matrix")),
            patch("mvp_api.is_api_web_workload", return_value=True),
            patch("mvp_api.LOKI_BASE_URL", ""),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock) as mock_llm,
        ):
            resp = await phase4_execute(app_log_alert)

        mock_llm.assert_not_called()
        assert resp.plan.action == "noop"
        assert "ERR_REA_LOG_SOURCE_UNAVAILABLE" in resp.plan.reasoning
        assert resp.lane == "app_log"

    async def test_loki_unavailable_sets_error_code_in_lane_meta(self, app_log_alert):
        """lane_meta must reflect the Loki error code for downstream observability."""
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("app_log", "matrix")),
            patch("mvp_api.is_api_web_workload", return_value=True),
            patch("mvp_api.LOKI_BASE_URL", ""),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
        ):
            resp = await phase4_execute(app_log_alert)

        assert resp.lane_meta.get("loki_unavailable") is True
        assert resp.lane_meta.get("error_code") == mvp_api.ERR_REA_LOG_SOURCE_UNAVAILABLE

    async def test_loki_escalate_returns_noop(self, app_log_alert):
        """Loki escalate_log_unavailable=True also triggers fail-closed noop."""
        surge_unavailable = MagicMock(
            ok=False,
            escalate_log_unavailable=True,
            reason="loki_connect_error",
            meta={},
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("app_log", "matrix")),
            patch("mvp_api.is_api_web_workload", return_value=True),
            patch("mvp_api.LOKI_BASE_URL", "http://loki.svc:3100"),
            patch(
                "mvp_api.evaluate_log_surge_sigma_bypass",
                new_callable=AsyncMock,
                return_value=surge_unavailable,
            ),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock) as mock_llm,
        ):
            resp = await phase4_execute(app_log_alert)

        mock_llm.assert_not_called()
        assert resp.plan.action == "noop"


# ===========================================================================
# Scenario 4 — Max Retries (loop exhaustion without convergence)
# ===========================================================================

class TestMaxRetries:
    """
    When phase5_verify always returns unhealthy, the loop runs MAX_LOOP_ITERATIONS
    times and terminates gracefully with converged=False.
    """

    async def test_loop_exhausts_after_max_iterations(self, cm_fault_alert):
        """Loop terminates at MAX_LOOP_ITERATIONS; converged=False."""
        persistent_plan = HighLevelRemediationPlan(
            action="rollout_restart",
            target_ref="nginx",
            namespace="lab-test",
            reasoning="Restart to recover from fault.",
        )

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=persistent_plan),
            patch(
                "mvp_api._execute_library_tool",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_exec,
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(False, "Deployment nginx/lab-test: desired=1 available=0 ready=0"),
            ) as mock_verify,
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock) as mock_shadow,
        ):
            resp = await phase4_execute(cm_fault_alert)

        assert resp.iterations == mvp_api.MAX_LOOP_ITERATIONS
        assert resp.converged is False
        assert resp.executed is True
        assert mock_exec.call_count == mvp_api.MAX_LOOP_ITERATIONS
        assert mock_verify.call_count == mvp_api.MAX_LOOP_ITERATIONS
        mock_shadow.assert_not_called()

    async def test_history_grows_across_iterations(self, cm_fault_alert):
        """Each iteration appends to the context — all 3 appear in the final LLM prompt."""
        persistent_plan = HighLevelRemediationPlan(
            action="rollout_restart",
            target_ref="nginx",
            namespace="lab-test",
            reasoning="Restart.",
        )
        captured: list[str] = []

        async def _capture_llm(system_prompt: str, user_message: str) -> HighLevelRemediationPlan:
            captured.append(system_prompt)
            return persistent_plan

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api.phase3_output", side_effect=_capture_llm),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(False, "desired=1 available=0 ready=0"),
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
        ):
            resp = await phase4_execute(cm_fault_alert)

        assert resp.iterations == 3
        # Iteration 1: no history
        assert "REMEDIATION HISTORY" not in captured[0]
        # Iteration 2: 1 outcome visible
        assert "Iteration 1" in captured[1]
        # Iteration 3: 2 outcomes visible
        assert "Iteration 2" in captured[2]

    async def test_noop_from_llm_terminates_loop_without_convergence(self, cm_fault_alert):
        """
        If LLM decides noop, the loop terminates immediately.
        converged=False: noop is NOT SDK-verified — it's the model's assessment only.
        resolution_state='incomplete'.
        """
        noop_plan = HighLevelRemediationPlan(
            action="noop",
            reasoning="Workload appears healthy — no action required.",
        )

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=noop_plan),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock) as mock_exec,
        ):
            resp = await phase4_execute(cm_fault_alert)

        assert resp.plan.action == "noop"
        assert resp.converged is False
        assert resp.resolution_state == "incomplete"
        assert resp.iterations == 1
        mock_exec.assert_not_called()


# ===========================================================================
# Unit tests — RemediationContext
# ===========================================================================

class TestRemediationContext:
    """Unit coverage for RemediationContext and to_prompt_block()."""

    def test_empty_context_returns_empty_block(self):
        ctx = RemediationContext(trace_id="abc", alertname="Test", namespace="ns")
        assert ctx.to_prompt_block() == ""

    def test_single_iteration_history_block(self):
        ctx = RemediationContext(trace_id="t1", alertname="OOMKilled", namespace="prod")
        ctx.iterations = 1
        ctx.observations.append(ObservationRecord(iteration=1, summary="Pod OOMKilled"))
        ctx.actions_taken.append(
            ActionRecord(
                iteration=1,
                action="patch_deployment_resource",
                target_ref="api",
                namespace="prod",
                reasoning="OOMKilled — increase memory limit",
            )
        )
        ctx.outcomes.append(OutcomeRecord(iteration=1, healthy=False, summary="desired=1 available=0"))

        block = ctx.to_prompt_block()
        assert "REMEDIATION HISTORY" in block
        assert "patch_deployment_resource" in block
        assert "UNHEALTHY" in block
        assert "END HISTORY" in block

    def test_healthy_outcome_shows_healthy_label(self):
        ctx = RemediationContext(trace_id="t2", alertname="X", namespace="ns")
        ctx.iterations = 1
        ctx.outcomes.append(OutcomeRecord(iteration=1, healthy=True, summary="desired=1 available=1"))
        block = ctx.to_prompt_block()
        assert "HEALTHY" in block

    def test_guidelines_always_present(self):
        ctx = RemediationContext(trace_id="t3", alertname="X", namespace="ns")
        ctx.iterations = 1
        ctx.observations.append(ObservationRecord(iteration=1, summary="alert"))
        block = ctx.to_prompt_block()
        assert "rollout_restart" in block  # guideline hint
        assert "Do not repeat" in block

    def test_multi_iteration_all_appear(self):
        ctx = RemediationContext(trace_id="t4", alertname="X", namespace="ns")
        ctx.iterations = 3
        for i in range(1, 4):
            ctx.observations.append(ObservationRecord(iteration=i, summary=f"obs {i}"))
            ctx.actions_taken.append(
                ActionRecord(iteration=i, action="rollout_restart", target_ref="svc", namespace="ns", reasoning="r")
            )
            ctx.outcomes.append(OutcomeRecord(iteration=i, healthy=False, summary=f"unhealthy {i}"))
        block = ctx.to_prompt_block()
        for i in range(1, 4):
            assert f"Iteration {i}" in block


# ===========================================================================
# Unit tests — OLLAMA_BASE_URL auto-detection (GAP-05)
# ===========================================================================

class TestVLLMUrlAutoDetection:
    """Validates _default_vllm_url() resolves correctly per environment."""

    def test_explicit_vllm_env_wins(self):
        from mvp_api import _default_vllm_url
        with patch.dict(os.environ, {"VLLM_BASE_URL": "http://custom-vllm:8000"}):
            assert _default_vllm_url() == "http://custom-vllm:8000"

    def test_ollama_env_falls_back_for_compat(self):
        from mvp_api import _default_vllm_url
        env = {"OLLAMA_BASE_URL": "http://compat-host:11434"}
        with patch.dict(os.environ, env, clear=False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VLLM_BASE_URL", None)
                result = _default_vllm_url()
        assert result == "http://compat-host:11434"

    def test_k8s_env_selects_host_docker_internal(self):
        from mvp_api import _default_vllm_url
        env = {"KUBERNETES_SERVICE_HOST": "10.96.0.1"}
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("VLLM_BASE_URL", "OLLAMA_BASE_URL")}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True):
            result = _default_vllm_url()
        assert result == "http://host.orb.internal:11434/v1"

    def test_local_dev_defaults_to_localhost_8000(self):
        from mvp_api import _default_vllm_url
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("VLLM_BASE_URL", "OLLAMA_BASE_URL", "KUBERNETES_SERVICE_HOST")}
        with patch.dict(os.environ, clean_env, clear=True):
            assert _default_vllm_url() == "http://localhost:11434/v1"


class TestHealthz:
    """GET /healthz — e2e_incident_matrix.sh reachability probe (curl -sf .../healthz)."""

    def test_healthz_returns_ok(self):
        from starlette.testclient import TestClient

        from mvp_api import app

        client = TestClient(app)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ===========================================================================
# Unit tests — get_resource_owner (OwnerReference traversal)
# ===========================================================================

class TestGetResourceOwner:
    """
    Validates the OwnerReference traversal chain without touching real K8s.
    All K8s API calls are replaced with MagicMock objects.
    """

    def _make_owner_ref(self, kind: str, name: str) -> MagicMock:
        ref = MagicMock()
        ref.kind = kind
        ref.name = name
        return ref

    def _make_pod(self, owner_refs: list) -> MagicMock:
        pod = MagicMock()
        pod.metadata.owner_references = owner_refs
        return pod

    def _make_rs(self, owner_refs: list) -> MagicMock:
        rs = MagicMock()
        rs.metadata.owner_references = owner_refs
        return rs

    async def test_pod_to_rs_to_deployment(self):
        """Pod → ReplicaSet → Deployment traversal returns ('Deployment', name)."""
        pod = self._make_pod([self._make_owner_ref("ReplicaSet", "nginx-rs-abc")])
        rs = self._make_rs([self._make_owner_ref("Deployment", "nginx")])

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod = AsyncMock(return_value=pod)
        mock_v1.api_client = AsyncMock()

        mock_apps = AsyncMock()
        mock_apps.read_namespaced_replica_set = AsyncMock(return_value=rs)
        mock_apps.api_client = AsyncMock()

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await get_resource_owner("nginx-7d9f-xk2", "lab-test")

        assert result == ("Deployment", "nginx")

    async def test_pod_direct_statefulset(self):
        """Pod owned directly by StatefulSet returns ('StatefulSet', name)."""
        pod = self._make_pod([self._make_owner_ref("StatefulSet", "postgres")])

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod = AsyncMock(return_value=pod)
        mock_v1.api_client = AsyncMock()

        mock_apps = AsyncMock()
        mock_apps.api_client = AsyncMock()

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await get_resource_owner("postgres-0", "db")

        assert result == ("StatefulSet", "postgres")

    async def test_standalone_pod_returns_none(self):
        """Pod with no ownerReferences returns None."""
        pod = self._make_pod([])  # no owners

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod = AsyncMock(return_value=pod)
        mock_v1.api_client = AsyncMock()

        mock_apps = AsyncMock()
        mock_apps.api_client = AsyncMock()

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await get_resource_owner("standalone-pod", "default")

        assert result is None

    async def test_pod_not_found_returns_none(self):
        """When pod lookup fails (404), return None gracefully."""
        from kubernetes_asyncio.client import ApiException

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        mock_v1.api_client = AsyncMock()

        mock_apps = AsyncMock()
        mock_apps.api_client = AsyncMock()

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await get_resource_owner("missing-pod", "ns")

        assert result is None

    async def test_daemonset_owner_returned_directly(self):
        """Pod owned by DaemonSet is returned directly without RS traversal."""
        pod = self._make_pod([self._make_owner_ref("DaemonSet", "fluentd")])

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod = AsyncMock(return_value=pod)
        mock_v1.api_client = AsyncMock()

        mock_apps = AsyncMock()
        mock_apps.api_client = AsyncMock()

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await get_resource_owner("fluentd-xk2", "kube-system")

        assert result == ("DaemonSet", "fluentd")


# ===========================================================================
# Unit tests — phase5_verify
# ===========================================================================

class TestPhase5Verify:
    """phase5_verify delegates owner resolution and performs kind-aware health check."""

    def _make_laned(self, pod: str = "nginx-7d9f-xk2", ns: str = "lab-test") -> LanedAlert:
        from mvp_api import Lane
        import uuid
        return LanedAlert(
            alertname="Test",
            namespace=ns,
            pod=pod,
            container="nginx",
            severity="critical",
            memory_limit="",
            message="",
            deployment_name="nginx",
            trace_id=str(uuid.uuid4()),
            lane=Lane.STATE,
        )

    async def test_noop_plan_returns_healthy(self):
        laned = self._make_laned()
        plan = HighLevelRemediationPlan(action="noop")
        healthy, summary = await phase5_verify(laned, plan)
        assert healthy is True
        assert "noop" in summary

    async def test_deployment_healthy_when_replicas_match(self):
        """Deployment with available=desired=ready > 0 is healthy."""
        laned = self._make_laned()
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="nginx", namespace="lab-test"
        )

        dep = MagicMock()
        dep.spec.replicas = 2
        dep.status.available_replicas = 2
        dep.status.ready_replicas = 2

        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        mock_apps.api_client = AsyncMock()

        with (
            patch("mvp_api.get_resource_owner", new_callable=AsyncMock, return_value=("Deployment", "nginx")),
            patch("mvp_api._load_k8s_config", new_callable=AsyncMock),
            patch("mvp_api.k8s_client") as mock_k8s,
        ):
            mock_k8s.AppsV1Api.return_value = mock_apps
            healthy, summary = await phase5_verify(laned, plan)

        assert healthy is True
        assert "desired=2 available=2 ready=2" in summary

    async def test_deployment_unhealthy_when_no_available_replicas(self):
        laned = self._make_laned()
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="nginx", namespace="lab-test"
        )

        dep = MagicMock()
        dep.spec.replicas = 1
        dep.status.available_replicas = 0
        dep.status.ready_replicas = 0

        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        mock_apps.api_client = AsyncMock()

        with (
            patch("mvp_api.get_resource_owner", new_callable=AsyncMock, return_value=("Deployment", "nginx")),
            patch("mvp_api._load_k8s_config", new_callable=AsyncMock),
            patch("mvp_api.k8s_client") as mock_k8s,
        ):
            mock_k8s.AppsV1Api.return_value = mock_apps
            healthy, summary = await phase5_verify(laned, plan)

        assert healthy is False

    async def test_owner_resolution_failure_falls_back_to_heuristic(self):
        """When get_resource_owner fails, laned.deployment_name is used."""
        laned = self._make_laned()
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="nginx", namespace="lab-test"
        )

        dep = MagicMock()
        dep.spec.replicas = 1
        dep.status.available_replicas = 1
        dep.status.ready_replicas = 1

        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment = AsyncMock(return_value=dep)
        mock_apps.api_client = AsyncMock()

        with (
            # Owner resolution raises → fallback to heuristic
            patch("mvp_api.get_resource_owner", new_callable=AsyncMock, side_effect=Exception("k8s down")),
            patch("mvp_api._load_k8s_config", new_callable=AsyncMock),
            patch("mvp_api.k8s_client") as mock_k8s,
        ):
            mock_k8s.AppsV1Api.return_value = mock_apps
            healthy, summary = await phase5_verify(laned, plan)

        assert healthy is True  # falls back to laned.deployment_name="nginx"

    async def test_k8s_api_error_returns_unhealthy(self):
        """When the deployment read itself fails, return (False, error_summary)."""
        laned = self._make_laned()
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="nginx", namespace="lab-test"
        )
        from kubernetes_asyncio.client import ApiException

        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        mock_apps.api_client = AsyncMock()

        with (
            patch("mvp_api.get_resource_owner", new_callable=AsyncMock, return_value=None),
            patch("mvp_api._load_k8s_config", new_callable=AsyncMock),
            patch("mvp_api.k8s_client") as mock_k8s,
        ):
            mock_k8s.AppsV1Api.return_value = mock_apps
            healthy, summary = await phase5_verify(laned, plan)

        assert healthy is False
        assert "verification error" in summary


# ===========================================================================
# Unit tests — Backoff integration
# ===========================================================================

class TestVerifyBackoff:
    """VERIFY_BACKOFF_SECONDS causes asyncio.sleep between execute and verify."""

    async def test_backoff_sleep_is_called_with_correct_duration(self, cm_fault_alert):
        """asyncio.sleep is invoked with VERIFY_BACKOFF_SECONDS value."""
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="nginx", namespace="lab-test"
        )

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(True, "healthy"),
            ),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 7.0),
            patch("mvp_api.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await phase4_execute(cm_fault_alert)

        mock_asyncio.sleep.assert_called_once_with(7.0)

    async def test_zero_backoff_skips_sleep(self, cm_fault_alert):
        """VERIFY_BACKOFF_SECONDS=0 must not call asyncio.sleep at all."""
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="nginx", namespace="lab-test"
        )

        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(True, "healthy"),
            ),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await phase4_execute(cm_fault_alert)

        mock_asyncio.sleep.assert_not_called()


# ===========================================================================
# Three-Lane Full-Pipeline Tests
# ===========================================================================
#
# These tests exercise the complete phase1_parse → phase2_transform →
# phase3_output (→ phase4_execute) pipeline for each canonical lane.
# External I/O (Ollama, K8s API, Loki, Redis) is mocked; business-logic
# branches (gate, enrichers, invariants) run real code where possible.
#
# Lane matrix:
#   RESOURCE  — 3-sigma gate: blocked short-circuit | skipped (falls through) | anomaly → execute
#   STATE     — No gate: direct LLM → execute → verify (convergence | exec_skipped)
#   APP_LOG   — Fail-closed: Loki unavailable (see TestFailClosedLoki) | surge ok | no surge


# ---------------------------------------------------------------------------
# Phase 1 parse — lane assignment
# ---------------------------------------------------------------------------

from mvp_api import phase1_parse


class TestPhase1Parse:
    """
    phase1_parse must assign the correct lane via resolve_proof_lane heuristics.
    Tests use real alertname patterns so the heuristics run live.
    """

    def test_oomkilled_assigns_state_lane(self):
        """KubePodOOMKilled → state_lane_heuristic match → Lane.STATE."""
        raw = AlertInput(
            alertname="KubePodOOMKilled",
            namespace="production",
            pod="api-server-7d9f8b-xk2",
            container="api-server",
            severity="critical",
        )
        laned = phase1_parse(raw)
        assert laned.lane is Lane.STATE
        # Deployment name strips the two trailing hash segments.
        assert laned.deployment_name == "api-server"

    def test_high_cpu_assigns_resource_lane(self):
        """Generic CPU alert with no state/app_log heuristic match → Lane.RESOURCE."""
        raw = AlertInput(
            alertname="HighCPUUsage",
            namespace="production",
            pod="worker-abc-def-xyz",
            container="worker",
            severity="warning",
        )
        laned = phase1_parse(raw)
        assert laned.lane is Lane.RESOURCE

    def test_http_error_rate_assigns_app_log_lane(self):
        """HttpErrorRate5xx triggers app_log_heuristic + is_api_web_workload → Lane.APP_LOG."""
        raw = AlertInput(
            alertname="HttpErrorRate5xx",
            namespace="production",
            pod="api-server-7d9f8b-xk2",
            container="api-server",
            severity="critical",
            message="sustained 503 errors on /checkout",
        )
        laned = phase1_parse(raw)
        assert laned.lane is Lane.APP_LOG

    def test_app_log_lane_downgrade_when_not_api_web(self):
        """
        When resolve_proof_lane returns 'app_log' but is_api_web_workload=False,
        phase1_parse downgrades to 'resource' (lane_source='api_web_guard').
        Prevents non-web workloads from entering the Loki log-surge path.
        """
        raw = AlertInput(
            alertname="GenericAppAlert",
            namespace="production",
            pod="batch-worker-abc-xyz",
            container="worker",
            severity="warning",
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("app_log", "annotation")),
            patch("mvp_api.is_api_web_workload", return_value=False),
        ):
            laned = phase1_parse(raw)

        assert laned.lane is Lane.RESOURCE
        assert laned.lane_source == "api_web_guard"


# ---------------------------------------------------------------------------
# Lane RESOURCE — end-to-end through phase4_execute
# ---------------------------------------------------------------------------

class TestResourceLaneE2E:
    """
    Lane RESOURCE full-pipeline through phase4_execute.

    Three critical branches:
      gate_blocked  → noop, no LLM, resolution_state='sigma_gate'
      gate_skipped  → LLM called, plan returned
      is_anomaly    → LLM → execute → verify → converged
    """

    @pytest.fixture
    def resource_alert(self) -> AlertInput:
        return AlertInput(
            alertname="HighMemoryUsage",
            namespace="multi-agent",
            pod="api-server-7d9f8b-xk2",
            container="api-server",
            severity="warning",
            memory_limit="512Mi",
        )

    async def test_gate_blocked_short_circuits_to_noop(self, resource_alert):
        """
        3-sigma gate blocked (z-score below threshold) → no LLM call, immediate noop.

        The RESOURCE lane must refuse to call the LLM when the metric is within
        normal range; lateral escalation to the executor would be a false positive.

        Asserts: plan.action='noop', converged=False, resolution_state='sigma_gate',
                 LLM never called.
        """
        gate_meta = {
            "gate": "3sigma",
            "metric_value_mib": 512.0,
            "is_anomaly": False,
            "z_score": 0.5,
            "gate_blocked": True,
        }
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("resource", "default")),
            patch.dict(mvp_api._ENRICHERS, {Lane.RESOURCE: AsyncMock(return_value=gate_meta)}),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock) as mock_llm,
        ):
            resp = await phase4_execute(resource_alert)

        assert resp.plan.action == "noop"
        assert resp.converged is False
        assert resp.resolution_state == "sigma_gate"
        assert resp.lane == "resource"
        assert resp.lane_meta.get("gate_blocked") is True
        mock_llm.assert_not_called()  # gate must short-circuit before LLM

    async def test_gate_skipped_falls_through_to_llm(self, resource_alert):
        """
        Gate skipped (e.g. Redis unavailable) → LLM is called; plan returned.

        gate_skipped must NOT short-circuit — the metric is unclassified, not
        within-range, so the LLM is the correct fallback.

        Asserts: LLM called exactly once, plan carries LLM's action.
        """
        plan_noop = HighLevelRemediationPlan(
            action="noop",
            reasoning="memory within expected range — 3-sigma gate skipped; conservative noop.",
        )
        gate_meta = {
            "gate": "3sigma",
            "metric_value_mib": 512.0,
            "gate_skipped": True,
        }
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("resource", "default")),
            patch.dict(mvp_api._ENRICHERS, {Lane.RESOURCE: AsyncMock(return_value=gate_meta)}),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan_noop) as mock_llm,
        ):
            resp = await phase4_execute(resource_alert)

        mock_llm.assert_called_once()
        assert resp.plan.action == "noop"
        assert resp.lane == "resource"
        assert resp.lane_meta.get("gate_skipped") is True

    async def test_anomaly_detected_executes_patch_and_converges(self, resource_alert):
        """
        is_anomaly=True path: LLM returns patch_deployment_resource →
        execute → verify healthy → converged=True in a single iteration.

        Validates that an anomalous RESOURCE alert travels the full mutation path.
        """
        gate_meta = {
            "gate": "3sigma",
            "metric_value_mib": 2048.0,
            "is_anomaly": True,
            "z_score": 4.7,
        }
        plan_patch = HighLevelRemediationPlan(
            action="patch_deployment_resource",
            target_ref="api-server",
            namespace="multi-agent",
            patch_json='{"spec":{"template":{"spec":{"containers":[{"name":"api-server","resources":{"limits":{"memory":"1Gi"}}}]}}}}',
            reasoning="3-sigma anomaly z=4.7; increasing memory limit.",
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("resource", "default")),
            patch.dict(mvp_api._ENRICHERS, {Lane.RESOURCE: AsyncMock(return_value=gate_meta)}),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan_patch),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(True, "Deployment api-server/multi-agent: desired=2 available=2 ready=2"),
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
        ):
            resp = await phase4_execute(resource_alert)

        assert resp.converged is True
        assert resp.iterations == 1
        assert resp.resolution_state == "converged"
        assert resp.plan.action == "patch_deployment_resource"
        assert resp.lane == "resource"
        assert resp.lane_meta.get("is_anomaly") is True


# ---------------------------------------------------------------------------
# Lane STATE — end-to-end through phase4_execute
# ---------------------------------------------------------------------------

class TestStateLaneE2E:
    """
    Lane STATE full-pipeline through phase4_execute.

    STATE lane has no statistical gate — execution follows immediately after LLM.
    Tests single-iteration convergence and the exec_skipped path outside lab mode.
    (Multi-iteration convergence is covered by TestTwoIterationRecovery.)
    """

    @pytest.fixture
    def oom_state_alert(self) -> AlertInput:
        return AlertInput(
            alertname="KubePodOOMKilled",
            namespace="multi-agent",
            pod="api-server-7d9f8b-xk2",
            container="api-server",
            severity="critical",
        )

    async def test_single_iteration_convergence(self, oom_state_alert):
        """
        STATE lane: LLM returns patch_deployment_resource on iteration 1,
        phase5_verify reports healthy → converged=True, iterations=1,
        resolution_state='converged', executed=True.
        """
        plan_patch = HighLevelRemediationPlan(
            action="patch_deployment_resource",
            target_ref="api-server",
            namespace="multi-agent",
            patch_json='{"spec":{"template":{"spec":{"containers":[{"name":"api-server","resources":{"limits":{"memory":"1Gi"}}}]}}}}',
            reasoning="OOMKilled: increasing memory limit by 50% (512Mi → 1Gi).",
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan_patch),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(True, "Deployment api-server/multi-agent: desired=2 available=2 ready=2"),
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
        ):
            resp = await phase4_execute(oom_state_alert)

        assert resp.converged is True
        assert resp.iterations == 1
        assert resp.resolution_state == "converged"
        assert resp.plan.action == "patch_deployment_resource"
        assert resp.lane == "state"
        assert resp.executed is True

    async def test_exec_skipped_outside_lab_mode(self, oom_state_alert):
        """
        When OMNI_ENV_MODE != 'lab', _execute_library_tool returns False without
        touching K8s → loop breaks with executed=False, converged=False,
        resolution_state='exec_skipped'.

        This is the safety gate that prevents arbitrary mutations outside the lab.
        """
        plan_patch = HighLevelRemediationPlan(
            action="patch_deployment_resource",
            target_ref="api-server",
            namespace="multi-agent",
            patch_json='{"spec":{"template":{"spec":{"containers":[{"name":"api-server","resources":{"limits":{"memory":"768Mi"}}}]}}}}',
            reasoning="OOMKilled outside lab",
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("state", "heuristic")),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan_patch),
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch.dict(os.environ, {"OMNI_ENV_MODE": "prod"}),
        ):
            resp = await phase4_execute(oom_state_alert)

        assert resp.executed is False
        assert resp.converged is False
        assert resp.resolution_state == "exec_skipped"
        assert resp.lane == "state"


# ---------------------------------------------------------------------------
# Lane APP_LOG — end-to-end through phase4_execute (happy path)
# ---------------------------------------------------------------------------

class TestAppLogLaneE2E:
    """
    Lane APP_LOG full-pipeline through phase4_execute — log-evidence-present paths.

    Fail-closed paths (loki_unavailable) are already covered by TestFailClosedLoki.
    These tests cover the two outcomes when Loki does respond:
      log_surge_ok=True  → LLM called → rollout_restart → execute → converged
      log_surge_ok=False → LLM called → noop (model withholds action) → incomplete
    """

    @pytest.fixture
    def app_log_5xx_alert(self) -> AlertInput:
        return AlertInput(
            alertname="HttpErrorRate5xx",
            namespace="production",
            pod="api-server-7d9f8b-xk2",
            container="api-server",
            severity="critical",
            message="sustained 503 errors on /checkout",
        )

    async def test_loki_surge_ok_calls_llm_and_converges(self, app_log_5xx_alert):
        """
        Loki returns log_surge_ok=True → LLM is called with APP_LOG instructions →
        rollout_restart plan → executed → verified healthy → converged.

        Key assertions:
          - LLM called exactly once (surge evidence unlocks LLM path)
          - lane_meta['log_surge_ok'] is True (APP_LOG enricher ran)
          - resolution_state='converged'
        """
        from workers.log_surge_probe import LogSurgeResult

        surge_result = LogSurgeResult(
            ok=True,
            reason="30% of 120 lines contain 5xx status codes",
            escalate_log_unavailable=False,
            meta={"error_count": 36, "total_lines": 120},
        )
        plan_restart = HighLevelRemediationPlan(
            action="rollout_restart",
            target_ref="api-server",
            namespace="production",
            reasoning="Sustained 5xx log surge detected — rollout_restart to cycle pods.",
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("app_log", "heuristic")),
            patch("mvp_api.is_api_web_workload", return_value=True),
            patch("mvp_api.LOKI_BASE_URL", "http://loki:3100"),
            patch(
                "mvp_api.evaluate_log_surge_sigma_bypass",
                new_callable=AsyncMock,
                return_value=surge_result,
            ),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan_restart) as mock_llm,
            patch("mvp_api._apply_invariants", return_value=(True, None)),
            patch("mvp_api._execute_library_tool", new_callable=AsyncMock, return_value=True),
            patch(
                "mvp_api.phase5_verify",
                new_callable=AsyncMock,
                return_value=(True, "Deployment api-server/production: desired=3 available=3 ready=3"),
            ),
            patch("mvp_api.VERIFY_BACKOFF_SECONDS", 0),
            patch("mvp_api._shadow_writeback", new_callable=AsyncMock),
        ):
            resp = await phase4_execute(app_log_5xx_alert)

        mock_llm.assert_called_once()
        assert resp.plan.action == "rollout_restart"
        assert resp.lane == "app_log"
        assert resp.lane_meta.get("log_surge_ok") is True
        assert resp.resolution_state == "converged"
        assert resp.converged is True

    async def test_loki_surge_ok_false_calls_llm_model_chooses_noop(self, app_log_5xx_alert):
        """
        Loki responds but log_surge_ok=False (error ratio below threshold) →
        loki_unavailable is NOT set → LLM is still called → model returns noop
        → resolution_state='incomplete'.

        Validates the distinction: fail-closed blocks LLM entirely;
        a negative-surge result lets the LLM make the final call.
        """
        from workers.log_surge_probe import LogSurgeResult

        no_surge_result = LogSurgeResult(
            ok=False,
            reason="only 2% of 50 lines contain 5xx — below min_ratio=0.3",
            escalate_log_unavailable=False,
            meta={"error_count": 1, "total_lines": 50},
        )
        plan_noop = HighLevelRemediationPlan(
            action="noop",
            reasoning="Log surge ratio below threshold — no action warranted.",
        )
        with (
            patch("mvp_api.resolve_proof_lane", return_value=("app_log", "heuristic")),
            patch("mvp_api.is_api_web_workload", return_value=True),
            patch("mvp_api.LOKI_BASE_URL", "http://loki:3100"),
            patch(
                "mvp_api.evaluate_log_surge_sigma_bypass",
                new_callable=AsyncMock,
                return_value=no_surge_result,
            ),
            patch("mvp_api._rag_enrich", new_callable=AsyncMock, return_value=""),
            patch("mvp_api.phase3_output", new_callable=AsyncMock, return_value=plan_noop) as mock_llm,
        ):
            resp = await phase4_execute(app_log_5xx_alert)

        mock_llm.assert_called_once()
        assert resp.plan.action == "noop"
        assert resp.lane == "app_log"
        assert resp.lane_meta.get("log_surge_ok") is False
        assert resp.converged is False
        assert resp.resolution_state == "incomplete"


# ===========================================================================
# Unit tests — llm_contract.py (validators + helpers)
# Targets: lines 73, 81, 83, 86, 88, 91, 93-96, 210-242, 250-273
# ===========================================================================

from pkg.autonomy.llm_contract import (
    map_high_level_plan_to_mutate,
    parse_high_level_plan_json,
)
import pytest as _pytest


class TestHighLevelRemediationPlanValidators:
    """
    HighLevelRemediationPlan validators — action allowlist + action-specific field rules.
    Each raises ValidationError (Pydantic) on bad input.
    """

    def test_invalid_action_raises(self):
        """Unknown action string must be rejected by _action_ok."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="action must be one of"):
            HighLevelRemediationPlan(action="delete_everything", namespace="ns", target_ref="x")

    def test_patch_deployment_resource_requires_patch_json(self):
        """patch_deployment_resource with empty patch_json must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="non-empty patch_json"):
            HighLevelRemediationPlan(
                action="patch_deployment_resource",
                target_ref="deploy",
                namespace="ns",
                patch_json="",
            )

    def test_patch_deployment_resource_requires_namespace_and_target(self):
        """patch_deployment_resource with blank namespace must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="requires namespace and target_ref"):
            HighLevelRemediationPlan(
                action="patch_deployment_resource",
                target_ref="",
                namespace="",
                patch_json='{"spec":{}}',
            )

    def test_patch_configmap_key_requires_configmap_key(self):
        """patch_configmap_key with empty configmap_key must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="requires configmap_key"):
            HighLevelRemediationPlan(
                action="patch_configmap_key",
                target_ref="my-cm",
                namespace="ns",
                configmap_key="",
            )

    def test_patch_configmap_key_requires_namespace_and_target(self):
        """patch_configmap_key with blank namespace or target_ref must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="requires namespace and target_ref"):
            HighLevelRemediationPlan(
                action="patch_configmap_key",
                target_ref="",
                namespace="",
                configmap_key="some-key",
            )

    def test_rollout_restart_requires_namespace_and_target(self):
        """rollout_restart with blank target_ref must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="requires namespace and target_ref"):
            HighLevelRemediationPlan(action="rollout_restart", target_ref="", namespace="")

    def test_apply_rbac_requires_namespace(self):
        """apply_rbac_least_privilege with blank namespace must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="requires namespace"):
            HighLevelRemediationPlan(
                action="apply_rbac_least_privilege", target_ref="binding", namespace=""
            )

    def test_apply_rbac_requires_target_ref(self):
        """apply_rbac_least_privilege with blank target_ref must raise."""
        from pydantic import ValidationError
        with _pytest.raises(ValidationError, match="requires target_ref"):
            HighLevelRemediationPlan(
                action="apply_rbac_least_privilege", target_ref="", namespace="ns"
            )

    def test_valid_noop_needs_no_extra_fields(self):
        """noop is valid with only action set."""
        plan = HighLevelRemediationPlan(action="noop")
        assert plan.action == "noop"


class TestMapHighLevelPlanToMutate:
    """map_high_level_plan_to_mutate — all action branches + noop/empty guards."""

    def test_noop_returns_none(self):
        plan = HighLevelRemediationPlan(action="noop")
        assert map_high_level_plan_to_mutate(plan) is None

    def test_rollout_restart_returns_correct_shape(self):
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="api-server", namespace="multi-agent"
        )
        result = map_high_level_plan_to_mutate(plan)
        assert result is not None
        assert result["tool_name"] == "k8s_rollout_restart"
        assert result["args"]["namespace"] == "multi-agent"
        assert result["args"]["deployment"] == "api-server"

    def test_rollout_restart_empty_ns_or_tgt_returns_none(self):
        """rollout_restart with empty namespace/target_ref after strip → None (safety guard)."""
        # Bypass validator by constructing with valid fields then checking the mapper directly.
        # The mapper has its own guard independent of the validator.
        plan = HighLevelRemediationPlan(
            action="rollout_restart", target_ref="deploy", namespace="ns"
        )
        # Temporarily override to test mapper guard (use model_construct to skip validator)
        bare = HighLevelRemediationPlan.model_construct(
            action="rollout_restart", target_ref="", namespace=""
        )
        assert map_high_level_plan_to_mutate(bare) is None

    def test_patch_deployment_resource_shape(self):
        plan = HighLevelRemediationPlan(
            action="patch_deployment_resource",
            target_ref="frontend",
            namespace="staging",
            patch_json='{"spec":{"replicas":3}}',
        )
        result = map_high_level_plan_to_mutate(plan)
        assert result["tool_name"] == "k8s_patch_resource"
        assert result["args"]["resource_type"] == "Deployment"
        assert result["args"]["name"] == "frontend"
        assert result["args"]["patch_json"] == '{"spec":{"replicas":3}}'

    def test_patch_configmap_key_shape(self):
        plan = HighLevelRemediationPlan(
            action="patch_configmap_key",
            target_ref="app-config",
            namespace="production",
            configmap_key="db_host",
            configmap_value="db.prod.svc",
        )
        result = map_high_level_plan_to_mutate(plan)
        assert result["tool_name"] == "k8s_create_or_patch_configmap"
        assert result["args"]["key"] == "db_host"
        assert result["args"]["value"] == "db.prod.svc"
        assert result["args"]["name"] == "app-config"

    def test_apply_rbac_least_privilege_shape(self):
        plan = HighLevelRemediationPlan(
            action="apply_rbac_least_privilege",
            target_ref="admin-binding",
            namespace="multi-agent",
        )
        result = map_high_level_plan_to_mutate(plan)
        assert result["tool_name"] == "k8s_apply_rbac_least_privilege"
        assert result["args"]["remove_cluster_admin_binding"] == "admin-binding"
        assert result["args"]["namespace"] == "multi-agent"

    def test_apply_rbac_empty_ns_defaults_to_multi_agent(self):
        """mapper falls back to 'multi-agent' when namespace is blank after strip."""
        bare = HighLevelRemediationPlan.model_construct(
            action="apply_rbac_least_privilege", target_ref="some-binding", namespace=""
        )
        result = map_high_level_plan_to_mutate(bare)
        assert result["args"]["namespace"] == "multi-agent"

    def test_apply_rbac_empty_tgt_defaults_to_omni_worker(self):
        """mapper falls back to 'omni-worker-cluster-admin' when target_ref is blank."""
        bare = HighLevelRemediationPlan.model_construct(
            action="apply_rbac_least_privilege", target_ref="", namespace="ns"
        )
        result = map_high_level_plan_to_mutate(bare)
        assert result["args"]["remove_cluster_admin_binding"] == "omni-worker-cluster-admin"


class TestParseHighLevelPlanJson:
    """parse_high_level_plan_json — valid JSON, markdown fences, embedded JSON, bad input."""

    def _valid_payload(self, **overrides) -> dict:
        base = {"action": "noop", "target_ref": "", "namespace": ""}
        base.update(overrides)
        return base

    def test_empty_string_returns_none(self):
        assert parse_high_level_plan_json("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_high_level_plan_json("   \n  ") is None

    def test_plain_json_noop(self):
        raw = json.dumps({"action": "noop", "target_ref": "", "namespace": ""})
        result = parse_high_level_plan_json(raw)
        assert result is not None
        assert result.action == "noop"

    def test_markdown_json_fence_stripped(self):
        """LLM output wrapped in ```json ... ``` must be unwrapped."""
        raw = '```json\n{"action": "noop", "target_ref": "", "namespace": ""}\n```'
        result = parse_high_level_plan_json(raw)
        assert result is not None
        assert result.action == "noop"

    def test_plain_backtick_fence_stripped(self):
        """LLM output wrapped in ``` ... ``` (no json tag) must also be unwrapped."""
        raw = '```\n{"action": "noop", "target_ref": "", "namespace": ""}\n```'
        result = parse_high_level_plan_json(raw)
        assert result is not None
        assert result.action == "noop"

    def test_json_embedded_in_prose(self):
        """JSON object embedded in surrounding text must be extracted."""
        raw = 'Here is my plan:\n{"action": "noop", "target_ref": "", "namespace": ""}\nEnd.'
        result = parse_high_level_plan_json(raw)
        assert result is not None
        assert result.action == "noop"

    def test_completely_invalid_string_returns_none(self):
        assert parse_high_level_plan_json("not json at all") is None

    def test_json_array_returns_none(self):
        """Top-level array (not dict) must return None."""
        assert parse_high_level_plan_json('[{"action": "noop"}]') is None

    def test_schema_mismatch_returns_none(self):
        """Valid JSON dict but fails Pydantic validation → None."""
        raw = json.dumps({"action": "unknown_action", "namespace": "ns", "target_ref": "x"})
        assert parse_high_level_plan_json(raw) is None

    def test_rollout_restart_roundtrip(self):
        """Full rollout_restart plan serialises and parses correctly."""
        payload = {
            "action": "rollout_restart",
            "target_ref": "api-server",
            "namespace": "multi-agent",
            "reasoning": "CrashLoop detected",
        }
        result = parse_high_level_plan_json(json.dumps(payload))
        assert result is not None
        assert result.action == "rollout_restart"
        assert result.target_ref == "api-server"
        assert result.namespace == "multi-agent"


# ===========================================================================
# Unit tests — k8s_describe_resource ConfigMap / Secret support
# ===========================================================================

from workers.k8s_cluster_tools import DescribeResourceArgs, tool_k8s_describe_resource
from workers.analyst_agentic_loop import (
    _broken_spec_first_round_instruction,
    _normalize_describe_resource_type,
    coerce_k8s_readonly_args,
)


class TestBrokenSpecPlannerHint:
    """_broken_spec_first_round_instruction — contract hint when evidence shows missing CM/Secret."""

    def test_empty_when_no_broken_spec_signal(self):
        batch = [{"probe": "k8s_clinical_pod_status", "raw": "phase=Running"}]
        assert _broken_spec_first_round_instruction(batch) == ""

    def test_includes_describe_and_cm_name_from_events(self):
        batch = [
            {
                "probe": "k8s_clinical_pod_events",
                "extracted_fact": {"namespace": "multi-agent", "kind": "PodEvents"},
                "raw": (
                    'Warning FailedMount: configmap "nginx-test-never-created-cm" not found'
                ),
            }
        ]
        hint = _broken_spec_first_round_instruction(batch)
        assert "k8s_describe_resource" in hint
        assert "nginx-test-never-created-cm" in hint
        assert "multi-agent" in hint


class TestNormalizeDescribeResourceType:
    """_normalize_describe_resource_type — all supported kinds + alias forms."""

    def test_configmap_lower(self):
        assert _normalize_describe_resource_type("configmap") == "ConfigMap"

    def test_configmaps_plural(self):
        assert _normalize_describe_resource_type("configmaps") == "ConfigMap"

    def test_configmap_exact_case(self):
        assert _normalize_describe_resource_type("ConfigMap") == "ConfigMap"

    def test_secret_lower(self):
        assert _normalize_describe_resource_type("secret") == "Secret"

    def test_secrets_plural(self):
        assert _normalize_describe_resource_type("secrets") == "Secret"

    def test_secret_exact_case(self):
        assert _normalize_describe_resource_type("Secret") == "Secret"

    def test_pod_still_works(self):
        assert _normalize_describe_resource_type("pod") == "Pod"

    def test_deployment_still_works(self):
        assert _normalize_describe_resource_type("deployments") == "Deployment"

    def test_unknown_returns_none(self):
        assert _normalize_describe_resource_type("Ingress") is None

    def test_none_input_returns_none(self):
        assert _normalize_describe_resource_type(None) is None


class TestCoerceReadonlyArgsConfigMap:
    """coerce_k8s_readonly_args handles ConfigMap/Secret via resource_type and alias fields."""

    def test_resource_type_configmap_normalised(self):
        out = coerce_k8s_readonly_args(
            "k8s_describe_resource",
            {"resource_type": "configmap", "name": "my-cm", "namespace": "ns"},
        )
        assert out["resource_type"] == "ConfigMap"

    def test_kind_field_configmap_coerced(self):
        out = coerce_k8s_readonly_args(
            "k8s_describe_resource",
            {"kind": "ConfigMap", "name": "my-cm", "namespace": "ns"},
        )
        assert out["resource_type"] == "ConfigMap"
        assert "kind" not in out

    def test_resource_type_secret_normalised(self):
        out = coerce_k8s_readonly_args(
            "k8s_describe_resource",
            {"resource_type": "secrets", "name": "my-secret", "namespace": "ns"},
        )
        assert out["resource_type"] == "Secret"

    def test_kind_field_secret_coerced(self):
        out = coerce_k8s_readonly_args(
            "k8s_describe_resource",
            {"kind": "secret", "name": "db-creds", "namespace": "prod"},
        )
        assert out["resource_type"] == "Secret"
        assert "kind" not in out

    def test_non_describe_tool_passthrough(self):
        """Other tools must not be touched."""
        args = {"resource_type": "configmap", "name": "x", "namespace": "y"}
        out = coerce_k8s_readonly_args("k8s_tail_logs", args)
        assert out == args


class TestDescribeResourceArgsLiteral:
    """DescribeResourceArgs Pydantic model accepts the five supported kinds."""

    def test_configmap_accepted(self):
        a = DescribeResourceArgs(resource_type="ConfigMap", name="my-cm", namespace="ns")
        assert a.resource_type == "ConfigMap"

    def test_secret_accepted(self):
        a = DescribeResourceArgs(resource_type="Secret", name="db-creds", namespace="ns")
        assert a.resource_type == "Secret"

    def test_invalid_kind_raises(self):
        from pydantic import ValidationError
        with _pytest.raises(ValidationError):
            DescribeResourceArgs(resource_type="Ingress", name="x", namespace="y")


class TestToolK8sDescribeResourceConfigMap:
    """tool_k8s_describe_resource — ConfigMap and Secret branches with mocked CoreV1Api."""

    async def test_configmap_describe_ok(self):
        """ConfigMap exists → output contains describe_ok kind=ConfigMap and snippet."""
        cm_obj = MagicMock()
        cm_obj.to_dict.return_value = {
            "metadata": {"name": "nginx-config", "namespace": "lab-test"},
            "data": {"nginx.conf": "server { listen 80; }"},
        }
        ev_obj = MagicMock()
        ev_obj.items = []

        mock_v1 = MagicMock()
        mock_v1.read_namespaced_config_map = AsyncMock(return_value=cm_obj)
        mock_v1.list_namespaced_event = AsyncMock(return_value=ev_obj)
        mock_v1.api_client = AsyncMock()

        mock_apps = MagicMock()
        mock_apps.api_client = AsyncMock()

        args = DescribeResourceArgs(
            resource_type="ConfigMap", name="nginx-config", namespace="lab-test"
        )

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await tool_k8s_describe_resource(ctx=None, args=args)

        assert "describe_ok kind=ConfigMap" in result
        assert "nginx-config" in result
        assert "nginx.conf" in result

    async def test_configmap_not_found_returns_api_error(self):
        """ConfigMap 404 → exception string for LLM (no raise)."""
        from kubernetes_asyncio.client import ApiException

        mock_v1 = MagicMock()
        mock_v1.read_namespaced_config_map = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        mock_v1.api_client = AsyncMock()

        mock_apps = MagicMock()
        mock_apps.api_client = AsyncMock()

        args = DescribeResourceArgs(
            resource_type="ConfigMap", name="nginx-test-never-created-cm", namespace="lab-test"
        )

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await tool_k8s_describe_resource(ctx=None, args=args)

        assert "404" in result

    async def test_secret_redacts_data_values(self):
        """Secret data values are replaced with <redacted>; key names are preserved."""
        secret_obj = MagicMock()
        secret_obj.to_dict.return_value = {
            "metadata": {"name": "db-creds", "namespace": "prod"},
            "data": {"password": "c2VjcmV0MTIz", "username": "YWRtaW4="},
            "string_data": {},
        }
        ev_obj = MagicMock()
        ev_obj.items = []

        mock_v1 = MagicMock()
        mock_v1.read_namespaced_secret = AsyncMock(return_value=secret_obj)
        mock_v1.list_namespaced_event = AsyncMock(return_value=ev_obj)
        mock_v1.api_client = AsyncMock()

        mock_apps = MagicMock()
        mock_apps.api_client = AsyncMock()

        args = DescribeResourceArgs(
            resource_type="Secret", name="db-creds", namespace="prod"
        )

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await tool_k8s_describe_resource(ctx=None, args=args)

        assert "describe_ok kind=Secret" in result
        # Key names present, raw base64 values must NOT appear
        assert "password" in result
        assert "c2VjcmV0MTIz" not in result
        assert "<redacted>" in result

    async def test_secret_not_found_returns_api_error(self):
        """Secret 404 → exception string for LLM (no raise)."""
        from kubernetes_asyncio.client import ApiException

        mock_v1 = MagicMock()
        mock_v1.read_namespaced_secret = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        mock_v1.api_client = AsyncMock()

        mock_apps = MagicMock()
        mock_apps.api_client = AsyncMock()

        args = DescribeResourceArgs(
            resource_type="Secret", name="missing-secret", namespace="prod"
        )

        with (
            patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
            patch("workers.k8s_cluster_tools.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = mock_apps
            result = await tool_k8s_describe_resource(ctx=None, args=args)

        assert "404" in result
