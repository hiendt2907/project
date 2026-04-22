"""Executor-side: EXECUTE_MUTATE — chỉ tool Kubernetes (SDK + kubectl argv), không mở echo/shell/Prom/Redis."""

from __future__ import annotations

import json
import logging
from typing import Any

from pkg.autonomous_actions import build_action_feedback_body, infer_exit_code_from_tool_output
from pkg.reasoning.reason_codes import (
    ERR_GOV_ENV_PROD_STRICT,
    ERR_GOV_NS_OUT_OF_BOUNDS,
    ERR_GOV_UNAUTHORIZED_MUTATION,
)
from workers.env_mode import is_dev_mode, namespace_allowed
from workers.k8s_tools import deployment_evidence_snapshot, execute_rollout_restart_from_pending
from workers.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

_FEEDBACK_TRUNC = 6000


def _trunc_feedback_text(s: str, max_len: int = _FEEDBACK_TRUNC) -> str:
    s = s or ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "\n[...truncated]"


# Tên gọi LLM / prompt khác tên đăng ký @register_tool (typed registry).
MUTATE_TOOL_REGISTRY_NAME: dict[str, str] = {
    "k8s_patch_deployment": "k8s_patch_resource",
    "k8s_scale_resource": "k8s_scale_deployment",
}

# Mutate-only: these tools are allowed in EXECUTE_MUTATE.
K8S_SDK_MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_patch_resource",
        "k8s_patch_configmap",
        "k8s_patch_secret",
        "k8s_create_or_patch_configmap",
        "k8s_apply_rbac_least_privilege",
        "k8s_delete_pod",
        "kubectl_cluster",
    }
)

# Read/query taxonomy is kept for routing + explainability; these are blocked in EXECUTE_MUTATE.
K8S_SDK_READONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "k8s_describe_resource",
        "k8s_tail_logs",
        "k8s_get_logs",
        "k8s_get_events",
        "k8s_list_resources",
        "k8s_check_endpoints",
        "k8s_get_deployment_state",
        "k8s_list_workload_pods",
        "k8s_get_pod_secret_refs",
        "k8s_get_secret_keys",
        "k8s_verify_rollout",
        "k8s_list_nodes",
        "k8s_node_conditions",
        "k8s_list_services",
        "k8s_list_ingress",
        "list_namespace_pods",
        "namespace_pods_top",
        "list_all_pods_sdk",
        "resolve_pod_identity",
        "resolve_deployment_identity",
        "inspect_pod_deep",
        "inspect_pod_details",
        "k8s_list_pods",
    }
)

# Introspection / filter: tên được phép + alias.
MUTATE_TOOL_ALLOWLIST: frozenset[str] = K8S_SDK_MUTATING_TOOL_NAMES | frozenset(MUTATE_TOOL_REGISTRY_NAME.keys())
READONLY_TOOL_ALLOWLIST: frozenset[str] = K8S_SDK_READONLY_TOOL_NAMES
_MUTATING_POLICY_GUARD_TOOLS: frozenset[str] = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_patch_resource",
        "k8s_patch_configmap",
        "k8s_patch_secret",
        "k8s_create_or_patch_configmap",
        "k8s_apply_rbac_least_privilege",
        "k8s_delete_pod",
        "kubectl_cluster",
    }
)


def _normalize_mutate_args_for_registry(reg_name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
    args = dict(raw_args or {})
    if reg_name != "k8s_describe_resource":
        return args
    if not args.get("resource_type"):
        kind = str(args.get("kind") or args.get("type") or "").strip().lower()
        mapping = {
            "pod": "Pod",
            "pods": "Pod",
            "deployment": "Deployment",
            "deploy": "Deployment",
            "service": "Service",
            "svc": "Service",
        }
        rt = mapping.get(kind)
        if rt:
            args["resource_type"] = rt
    # Planner sometimes emits pod/deployment/service fields instead of generic name.
    if not args.get("name"):
        for k in ("name", "pod", "deployment", "service"):
            v = str(args.get(k) or "").strip()
            if v:
                args["name"] = v
                break
    return args


async def run_execute_mutate_tool(
    ctx: Any,
    *,
    tool_name: str,
    args: dict[str, Any],
    trace_id: str,
) -> tuple[str, int]:
    """Returns (combined_output, exit_code)."""
    name = str(tool_name or "").strip()
    reg_name = MUTATE_TOOL_REGISTRY_NAME.get(name, name)
    ws = getattr(ctx, "settings", None)
    force_nsenter = bool(getattr(ws, "omni_executor_force_nsenter", False)) if ws is not None else False
    if force_nsenter and reg_name != "kubectl_cluster":
        msg = (
            "[DATA] error\n[DIAGNOSIS] reason_code="
            f"{ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"executor_policy_denied: tool {name!r} resolved={reg_name!r} "
            "must route through kubectl_cluster with nsenter host wrapper."
        )
        return msg, 1
    unrestricted = bool(getattr(ws, "omni_unrestricted_tool_execution", True))
    if unrestricted:
        fn_any = TOOL_REGISTRY.get(reg_name) or TOOL_REGISTRY.get(name)
        if fn_any is None:
            return f"[DATA] error\n[DIAGNOSIS] Unknown tool {name!r} resolved={reg_name!r}", 1
        try:
            run_name = reg_name if TOOL_REGISTRY.get(reg_name) is not None else name
            run_args = _normalize_mutate_args_for_registry(run_name, args)
            out = await fn_any(ctx, run_args)
            s = str(out)
            return s, infer_exit_code_from_tool_output(s)
        except Exception as e:
            logger.exception("unrestricted_execute tool %s: %s", name, e)
            return f"[DATA] error\n[DIAGNOSIS] {e!s}", 1
    if reg_name in READONLY_TOOL_ALLOWLIST:
        msg = (
            f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"read_only_tool_blocked={name!r} resolved={reg_name!r} channel=EXECUTE_MUTATE"
        )
        return msg, 1
    if reg_name not in K8S_SDK_MUTATING_TOOL_NAMES:
        msg = (
            f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"tool={name!r} resolved={reg_name!r} not in mutate-only allowlist."
        )
        return msg, 1
    if reg_name not in TOOL_REGISTRY:
        msg = (
            f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"Kubernetes tool {reg_name!r} missing from TOOL_REGISTRY "
            f"(from {name!r}) — deployment bug."
        )
        return msg, 1
    if ws is not None and not is_dev_mode(ws) and reg_name in _MUTATING_POLICY_GUARD_TOOLS:
        ns = str((args or {}).get("namespace") or "").strip()
        if not ns:
            return (
                "[DATA] error\n[DIAGNOSIS] "
                f"reason_code={ERR_GOV_ENV_PROD_STRICT} "
                "prod_mode_policy_denied: mutating tool requires args.namespace."
            ), 1
        if not namespace_allowed(ws, ns):
            return (
                f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_NS_OUT_OF_BOUNDS} "
                f"prod_mode_policy_denied: namespace {ns!r} not allowed."
            ), 1
    if name == "k8s_rollout_restart":
        ns = str((args or {}).get("namespace") or "").strip()
        dep = str((args or {}).get("deployment") or (args or {}).get("name") or "").strip()
        if not ns or not dep:
            return "[DATA] error\n[DIAGNOSIS] k8s_rollout_restart requires namespace and deployment in args.", 1
        snap = (args or {}).get("evidence_snapshot")
        if not isinstance(snap, dict) or snap.get("deployment_generation") is None:
            snap = await deployment_evidence_snapshot(ns, dep)
        data: dict[str, Any] = {
            "kind": "k8s_rollout_restart",
            "namespace": ns,
            "deployment": dep,
            "trace_id": trace_id,
            "evidence_snapshot": snap,
        }
        out = await execute_rollout_restart_from_pending(ctx, data)
        return out, infer_exit_code_from_tool_output(out)

    fn = TOOL_REGISTRY.get(reg_name)
    if fn is None:
        return f"[DATA] error\n[DIAGNOSIS] Unknown tool {reg_name!r} (from {name!r})", 1
    try:
        norm_args = _normalize_mutate_args_for_registry(reg_name, args)
        out = await fn(ctx, norm_args)
        s = str(out)
        return s, infer_exit_code_from_tool_output(s)
    except Exception as e:
        logger.exception("autonomous_execute tool %s: %s", name, e)
        return f"[DATA] error\n[DIAGNOSIS] {e!s}", 1


async def publish_action_feedback(
    ctx: Any,
    *,
    trace_id: str,
    tool_name: str,
    correlation_id: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    status: str = "ok",
    skipped_reason: str | None = None,
    mutate_args: dict[str, Any] | None = None,
) -> None:
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    if k is None or ws is None:
        return
    topic = getattr(ws, "kafka_topic_action_feedback", "omni-action-feedback")
    out_s = _trunc_feedback_text(stdout) if exit_code != 0 else (stdout or "")
    err_s = _trunc_feedback_text(stderr) if exit_code != 0 else (stderr or "")
    body = build_action_feedback_body(
        trace_id=trace_id,
        tool_name=tool_name,
        correlation_id=correlation_id,
        stdout=out_s,
        stderr=err_s,
        exit_code=exit_code,
        status=status,
        skipped_reason=skipped_reason,
        mutate_args=mutate_args,
    )
    try:
        await k.send_dict(topic, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_feedback_published trace=%s exit_code=%s status=%s",
            trace_id,
            exit_code,
            status,
        )
    except Exception as e:
        logger.warning("action_feedback publish failed: %s", e)
