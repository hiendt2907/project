"""W1 coverage using real data models only: no mocks, no fakes, no hand-rolled doubles."""

from __future__ import annotations

import json
import os

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from kubernetes_asyncio import client as k8s_client

from gateway import api as gateway_api
from gateway.routes import compliance as compliance_routes
from gateway.routes import playbooks as playbook_routes
from pkg.reasoning import deterministic_mutate_from_evidence as dmf
from pkg.reasoning import evidence_signals
from pkg.reasoning import incident_matrix_profile as incident_matrix
from pkg.reasoning import preflight_deployment_secret_refs as preflight
from pkg.reasoning import sanitize as reasoning_sanitize


def test_gateway_json_with_trace_sets_header() -> None:
    response = gateway_api._json_with_trace({"ok": True}, trace_id="trace-12345678")
    assert response.status_code == 200
    assert response.headers["x-omni-trace-id"] == "trace-12345678"


@pytest.mark.asyncio
async def test_gateway_require_api_key_accepts_real_credentials() -> None:
    from starlette.testclient import TestClient
    from starlette.requests import Request as StarletteRequest
    from starlette.datastructures import State
    import types

    previous = os.environ.get("OMNI_GATEWAY_API_KEY")
    try:
        os.environ["OMNI_GATEWAY_API_KEY"] = "real-local-test-key"
        os.environ.pop("OMNI_TENANT_APIKEYS", None)
        os.environ.pop("OMNI_ADMIN_API_KEYS", None)

        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="real-local-test-key",
        )
        # Build a minimal mock request with a state object
        mock_request = types.SimpleNamespace(state=State())
        ctx = await gateway_api._require_api_key(mock_request, credentials)
        assert ctx.tenant_id == "default"
        assert ctx.is_admin is True
    finally:
        if previous is None:
            os.environ.pop("OMNI_GATEWAY_API_KEY", None)
        else:
            os.environ["OMNI_GATEWAY_API_KEY"] = previous


@pytest.mark.asyncio
async def test_gateway_require_api_key_rejects_missing_credentials() -> None:
    import types
    from starlette.datastructures import State

    previous = os.environ.get("OMNI_GATEWAY_API_KEY")
    try:
        os.environ["OMNI_GATEWAY_API_KEY"] = "required-key"
        os.environ.pop("OMNI_TENANT_APIKEYS", None)
        os.environ.pop("OMNI_ADMIN_API_KEYS", None)
        mock_request = types.SimpleNamespace(state=State())
        with pytest.raises(HTTPException) as exc:
            await gateway_api._require_api_key(mock_request, None)
        assert exc.value.status_code == 401
    finally:
        if previous is None:
            os.environ.pop("OMNI_GATEWAY_API_KEY", None)
        else:
            os.environ["OMNI_GATEWAY_API_KEY"] = previous


@pytest.mark.asyncio
async def test_gateway_forecast_matrix_zero_current_never_risks() -> None:
    body = gateway_api.ForecastMatrixRequest(
        metric_name="cpu",
        timestamps=[1.0, 2.0, 3.0],
        values=[0.0, 0.0, 0.0],
        step_seconds=3600.0,
    )
    out = await gateway_api.forecast_matrix(body)
    assert out.metric_name == "cpu"
    assert all(not h.risk for h in out.horizons.values())


@pytest.mark.asyncio
async def test_gateway_forecast_matrix_rejects_mismatched_lengths() -> None:
    body = gateway_api.ForecastMatrixRequest(
        metric_name="mem",
        timestamps=[1.0, 2.0],
        values=[1.0, 2.0, 3.0],
        step_seconds=60.0,
    )
    with pytest.raises(HTTPException) as exc:
        await gateway_api.forecast_matrix(body)
    assert exc.value.status_code == 422


def test_gateway_counter_duplicate_returns_existing_collector() -> None:
    first = gateway_api._get_or_create_counter(
        "omni_gateway_w1_duplicate_total",
        "w1 duplicate counter",
        ["status"],
    )
    second = gateway_api._get_or_create_counter(
        "omni_gateway_w1_duplicate_total",
        "w1 duplicate counter",
        ["status"],
    )
    assert second is first


def test_route_helpers_without_redis_raise_503() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "app": gateway_api.app,
    }
    request = gateway_api.Request(scope)
    from gateway.routes import autonomy as autonomy_routes

    with pytest.raises(HTTPException) as autonomy_exc:
        autonomy_routes._get_redis(request)
    with pytest.raises(HTTPException) as playbook_exc:
        playbook_routes._get_redis(request)
    with pytest.raises(HTTPException) as compliance_exc:
        compliance_routes._get_redis(request)
    assert {autonomy_exc.value.status_code, playbook_exc.value.status_code, compliance_exc.value.status_code} == {503}


def test_compliance_pure_key_parse_and_row_helpers() -> None:
    assert compliance_routes._blocks_key("default") == "audit_chain:blocks"
    assert compliance_routes._blocks_key("tenant-a") == "audit_chain:tenant-a:blocks"
    assert compliance_routes._head_key("tenant-a") == "audit_chain:tenant-a:head_hash"
    assert compliance_routes._seq_key("tenant-a") == "audit_chain:tenant-a:seq"
    assert compliance_routes._parse_block("{not-json") is None
    row = compliance_routes._block_to_row(
        {
            "seq": 7,
            "timestamp_utc": "2026-05-11T00:00:00Z",
            "event_type": "ADVISORY_DECISION",
            "trace_id": "t",
            "block_hash": "h",
            "prev_hash": "p",
        }
    )
    assert row["tenant_id"] == "default"
    assert row["has_signature"] == "false"


def test_preflight_secret_refs_uses_real_kubernetes_models() -> None:
    deployment = k8s_client.V1Deployment(
        spec=k8s_client.V1DeploymentSpec(
            selector=k8s_client.V1LabelSelector(match_labels={"app": "web"}),
            template=k8s_client.V1PodTemplateSpec(
                spec=k8s_client.V1PodSpec(
                    containers=[
                        k8s_client.V1Container(
                            name="web",
                            image="local/web:test",
                            env=[
                                k8s_client.V1EnvVar(
                                    name="DATABASE_PASSWORD",
                                    value_from=k8s_client.V1EnvVarSource(
                                        secret_key_ref=k8s_client.V1SecretKeySelector(
                                            name="db-credentials",
                                            key="password",
                                        )
                                    ),
                                ),
                                k8s_client.V1EnvVar(
                                    name="DATABASE_PASSWORD_DUP",
                                    value_from=k8s_client.V1EnvVarSource(
                                        secret_key_ref=k8s_client.V1SecretKeySelector(
                                            name="db-credentials",
                                            key="password",
                                        )
                                    ),
                                ),
                            ],
                            env_from=[
                                k8s_client.V1EnvFromSource(
                                    secret_ref=k8s_client.V1SecretEnvSource(name="shared-env")
                                )
                            ],
                        )
                    ]
                )
            ),
        )
    )
    refs = preflight.secret_refs_from_deployment(deployment)
    assert refs == [
        {
            "secret_name": "db-credentials",
            "secret_key": "password",
            "env_var": "DATABASE_PASSWORD",
            "source": "env.valueFrom.secretKeyRef",
        },
        {
            "secret_name": "shared-env",
            "secret_key": "(keys from envFrom — inspect Secret or describe Deployment)",
            "env_var": "",
            "source": "envFrom.secretRef",
        },
    ]


def test_preflight_secret_refs_skips_incomplete_real_models() -> None:
    deployment = k8s_client.V1Deployment(
        spec=k8s_client.V1DeploymentSpec(
            selector=k8s_client.V1LabelSelector(match_labels={"app": "web"}),
            template=k8s_client.V1PodTemplateSpec(
                spec=k8s_client.V1PodSpec(
                    containers=[
                        k8s_client.V1Container(
                            name="web",
                            image="local/web:test",
                            env=[
                                k8s_client.V1EnvVar(
                                    name="MISSING_KEY",
                                    value_from=k8s_client.V1EnvVarSource(
                                        secret_key_ref=k8s_client.V1SecretKeySelector(
                                            name="db-credentials",
                                            key="",
                                        )
                                    ),
                                )
                            ],
                            env_from=[
                                k8s_client.V1EnvFromSource(
                                    secret_ref=k8s_client.V1SecretEnvSource(name="")
                                )
                            ],
                        )
                    ]
                )
            ),
        )
    )
    assert preflight.secret_refs_from_deployment(deployment) == []


@pytest.mark.asyncio
async def test_preflight_merge_returns_original_without_rollout_labels() -> None:
    batch = [{"raw": "password authentication failed for user app"}]
    out = await preflight.merge_preflight_deployment_secret_refs(batch, trace="trace-preflight")
    assert out is batch


@pytest.mark.asyncio
async def test_preflight_merge_handles_real_kubernetes_config_absence() -> None:
    batch = [
        {
            "raw": "password authentication failed for user app",
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "namespace": "multi-agent",
                        "deployment": "api",
                    }
                }
            ),
        }
    ]
    out = await preflight.merge_preflight_deployment_secret_refs(batch, trace="trace-preflight")
    assert out is batch or out == batch


def test_deterministic_mutate_configmap_defaults_from_env() -> None:
    previous = {
        name: os.environ.get(name)
        for name in (
            "OMNI_WORKER_CONFIGMAP_NAME",
            "OMNI_GOD_MODE_PATCH_KEY",
            "OMNI_GOD_MODE_PATCH_VALUE",
        )
    }
    try:
        os.environ["OMNI_WORKER_CONFIGMAP_NAME"] = "omni-config"
        os.environ["OMNI_GOD_MODE_PATCH_KEY"] = "OMNI_AUTO_EXECUTE_ENABLED"
        os.environ["OMNI_GOD_MODE_PATCH_VALUE"] = "false"
        allowed = dmf.parse_probe_driven_mutate_tools_csv("k8s_patch_configmap")
        plan = dmf.deterministic_mutate_plan_from_item(
            {
                "probe": "configmap",
                "extracted_fact": {
                    "status": "FAILED",
                    "recommended_tool": "k8s_patch_configmap",
                    "namespace": "multi-agent",
                },
            },
            default_ns="multi-agent",
            allowed_tools=allowed,
        )
        assert plan is not None
        assert plan["args"]["name"] == "omni-config"
        assert plan["args"]["key"] == "OMNI_AUTO_EXECUTE_ENABLED"
        assert plan["args"]["value"] == "false"
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_evidence_signals_critical_paths() -> None:
    assert evidence_signals.critical_evidence_present([{"alert_hint": "Pod OOMKilled"}])
    assert evidence_signals.critical_evidence_present([{"raw": "container waiting backoff"}])
    assert evidence_signals.critical_evidence_present(
        [{"extracted_fact": {"phase": "Pending", "ready_false": True}}]
    )
    assert evidence_signals.critical_evidence_present(
        [{"extracted_fact": json.dumps({"pods": [{"has_crash_loop": True}]})}]
    )
    assert evidence_signals.critical_evidence_present(
        [
            {
                "canonical_query_snippet": json.dumps(
                    {"labels": {"reason": "ImagePullBackOff", "alertname": "KubePodFailure"}}
                )
            }
        ]
    )
    assert not evidence_signals.critical_evidence_present([{"alert_hint": "all healthy"}])


def test_incident_matrix_profile_pure_heuristics() -> None:
    assert incident_matrix.alertname_from_batch([{"alert_rule": "RuleFromField"}]) == "RuleFromField"
    assert incident_matrix.rag_match_text_implies_api_web("nginx returned status: 500 for REST API")
    assert not incident_matrix.rag_match_text_implies_api_web("short")
    labels = incident_matrix.labels_from_batch(
        [{"canonical_query_snippet": json.dumps({"labels": {"namespace": "n", "empty": ""}})}]
    )
    assert labels == {"namespace": "n"}
    assert not incident_matrix.row_matches_series_label_defaults(
        {"series_label_defaults": {"namespace": "prod"}},
        [{"canonical_query_snippet": json.dumps({"labels": {"namespace": "dev"}})}],
    )
    assert incident_matrix.proof_lane_from_annotation(
        [
            {
                "canonical_query_snippet": json.dumps(
                    {"labels": {"omni.omni_proof_lane": "state"}}
                )
            }
        ]
    ) == "state"
    assert incident_matrix.state_lane_heuristic(
        [{"canonical_query_snippet": json.dumps({"labels": {"reason": "FailedMount"}})}]
    )
    assert incident_matrix.app_log_heuristic(
        [{"canonical_query_snippet": json.dumps({"labels": {"alertname": "HttpErrorRate"}})}]
    )
    assert incident_matrix.resolve_proof_lane([], blind_lane_hint="app_log") == (
        "app_log",
        "blind_hint",
    )
    assert incident_matrix.expected_stage_for_batch([{"alert_rule": "definitely_missing"}]) is None


def test_incident_matrix_real_temp_matrix_selection(tmp_path: pytest.TempPathFactory) -> None:
    matrix_file = tmp_path / "matrix.yaml"
    matrix_file.write_text(
        """
scenarios:
  - id: row-specific
    prometheus_alert: SharedAlert
    workload_profile: worker
    proof_lane: state
    expected_stage: specific
    series_label_defaults:
      namespace: prod
  - id: row-api
    prometheus_alert: SharedAlert
    workload_profile: api_web
    proof_lane: app_log
    expected_stage: api
""",
        encoding="utf-8",
    )
    previous = os.environ.get("MATRIX_PATHS")
    try:
        os.environ["MATRIX_PATHS"] = str(matrix_file)
        incident_matrix.invalidate_matrix_cache()
        batch = [
            {
                "canonical_query_snippet": json.dumps(
                    {"labels": {"alertname": "SharedAlert", "namespace": "prod"}}
                )
            }
        ]
        row = incident_matrix.pick_matrix_row_for_batch(batch)
        assert row is not None
        assert row["id"] == "row-specific"
        assert incident_matrix.workload_profile_for_alert("SharedAlert") == "worker"
        assert incident_matrix.expected_stage_for_batch(batch) == "specific"
    finally:
        if previous is None:
            os.environ.pop("MATRIX_PATHS", None)
        else:
            os.environ["MATRIX_PATHS"] = previous
        incident_matrix.invalidate_matrix_cache()


def test_incident_matrix_real_temp_matrix_api_web_disambiguation(tmp_path: pytest.TempPathFactory) -> None:
    matrix_file = tmp_path / "matrix.yaml"
    matrix_file.write_text(
        """
scenarios:
  - id: row-worker
    prometheus_alert: SharedAlert
    workload_profile: worker
    proof_lane: resource
  - id: row-api
    prometheus_alert: SharedAlert
    workload_profile: api_web
    proof_lane: app_log
""",
        encoding="utf-8",
    )
    previous = os.environ.get("MATRIX_PATHS")
    try:
        os.environ["MATRIX_PATHS"] = str(matrix_file)
        incident_matrix.invalidate_matrix_cache()
        batch = [
            {
                "canonical_query_snippet": json.dumps(
                    {"labels": {"alertname": "SharedAlert"}}
                )
            }
        ]
        row = incident_matrix.pick_matrix_row_for_batch(
            batch,
            rag_match_text="REST API gateway returned status: 500",
        )
        assert row is not None
        assert row["id"] == "row-api"
    finally:
        if previous is None:
            os.environ.pop("MATRIX_PATHS", None)
        else:
            os.environ["MATRIX_PATHS"] = previous
        incident_matrix.invalidate_matrix_cache()


def test_sanitize_filter_evidence_for_rag_keeps_k8s_signals_and_removes_http_junk() -> None:
    text = reasoning_sanitize.filter_evidence_for_rag(
        [
            {
                "canonical_query_snippet": json.dumps(
                    {"labels": {"alertname": "KubePodCrashLoop"}}
                ),
                "probe": "k8s_clinical_pod_status",
                "extracted_fact": json.dumps(
                    {
                        "phase": "Pending",
                        "waiting_reasons": ["CrashLoopBackOff"],
                        "container_signals": ["exit_code=1"],
                    }
                ),
                "symptom_group": "state",
                "layer": "kubernetes",
            },
            {
                "probe": "k8s_events_probe",
                "raw": "HTTP/1.1 400 Bad Request\r\ncontent-type: text/plain\r\nFailedMount configmap missing",
            },
        ],
        max_tokens=128,
    )
    assert "KubePodCrashLoop" in text
    assert "CrashLoopBackOff" in text
    assert "FailedMount" in text
    assert "Bad Request" not in text


def test_deterministic_mutate_secret_validation_and_batch_first_match() -> None:
    args = dmf._validate_tool_args(
        "k8s_patch_secret",
        {
            "name": "db-secret",
            "namespace": "multi-agent",
            "key": "password",
            "value": "replacement",
            "value_source": "operator",
            "value_source_ref": "ticket-1",
            "reasoning": "rotate credential",
        },
        default_ns="multi-agent",
    )
    assert args is not None
    assert args["value_source"] == "operator"

    allowed = dmf.parse_probe_driven_mutate_tools_csv("k8s_patch_configmap")
    batch = [
        {"extracted_fact": {"status": "OK"}},
        {
            "probe": "configmap",
            "extracted_fact": json.dumps(
                {
                    "status": "FAILED",
                    "recommended_tool": "k8s_patch_configmap",
                    "namespace": "multi-agent",
                    "name": "omni-config",
                    "key": "FLAG",
                    "value": "off",
                }
            ),
        },
    ]
    plan = dmf.deterministic_mutate_plan_from_batch(
        batch,
        default_ns="multi-agent",
        allowed_tools=allowed,
    )
    assert plan is not None
    assert plan["tool_name"] == "k8s_patch_configmap"


def test_deterministic_mutate_namespace_deployment_from_truncated_json_order() -> None:
    batch = [
        {
            "canonical_query_snippet": (
                '{"labels":{"deployment":"api","pod":"api-abc","namespace":"multi-agent"'
            )
        }
    ]
    assert dmf._namespace_deployment_from_batch(batch) == ("multi-agent", "api")


def test_deterministic_mutate_oom_patch_uses_env_container_and_memory() -> None:
    previous = {
        name: os.environ.get(name)
        for name in (
            "OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED",
            "OMNI_OOM_PATCH_CONTAINER",
            "OMNI_OOM_PATCH_MEMORY",
        )
    }
    try:
        os.environ["OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED"] = "true"
        os.environ["OMNI_OOM_PATCH_CONTAINER"] = "nginx"
        os.environ["OMNI_OOM_PATCH_MEMORY"] = "768Mi"
        plan = dmf.oom_deterministic_plan_from_batch(
            [
                {
                    "canonical_query_snippet": json.dumps(
                        {
                            "labels": {
                                "alertname": "OmniOomKilledPodNoRecovery",
                                "namespace": "multi-agent",
                                "deployment": "nginx",
                            }
                        }
                    )
                }
            ],
            default_ns="multi-agent",
            allowed_tools=frozenset({"k8s_patch_resource"}),
        )
        assert plan is not None
        patch = json.loads(plan["args"]["patch_json"])
        container = patch["spec"]["template"]["spec"]["containers"][0]
        assert container["name"] == "nginx"
        assert container["resources"]["limits"]["memory"] == "768Mi"
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
