"""Lab chaos k8s_patch_secret args: provenance fields + settings-backed value."""

from pkg.reasoning.deterministic_mutate_from_evidence import (
    _validate_tool_args,
    chaos_credential_lab_autofix_plan_from_batch,
)


def test_validate_k8s_patch_secret_passes_value_source_fields() -> None:
    d = _validate_tool_args(
        "k8s_patch_secret",
        {
            "namespace": "multi-agent",
            "name": "chaos-pg-secret",
            "key": "APP_PASSWORD",
            "value": "secret-value",
            "value_source": "lab_chaos_autofix",
            "value_source_ref": "OMNI_CHAOS_PG_APP_PASSWORD",
            "reasoning": "r",
        },
        default_ns="multi-agent",
    )
    assert d is not None
    assert d["value_source"] == "lab_chaos_autofix"
    assert d["value_source_ref"] == "OMNI_CHAOS_PG_APP_PASSWORD"


def test_chaos_credential_lab_autofix_includes_provenance() -> None:
    class _Ws:
        lab_chaos_credential_autofix_enabled = True
        chaos_pg_app_password = "chaos-app-pass-2025"
        chaos_pg_secret_name = "chaos-pg-secret"
        chaos_pg_password_key = "APP_PASSWORD"
        chaos_lab_namespace = "multi-agent"

    batch = [
        {
            "raw": "password authentication failed for user chaos_app",
            "canonical_query_snippet": '{"labels":{"namespace":"multi-agent","deployment":"chaos-victim"}}',
        }
    ]
    plan = chaos_credential_lab_autofix_plan_from_batch(
        batch,
        default_ns="multi-agent",
        allowed_tools=frozenset({"k8s_patch_secret"}),
        ws=_Ws(),
    )
    assert plan is not None
    args = plan["args"]
    assert args["value"] == "chaos-app-pass-2025"
    assert args["value_source"] == "lab_chaos_autofix"
    assert args["value_source_ref"] == "OMNI_CHAOS_PG_APP_PASSWORD"
