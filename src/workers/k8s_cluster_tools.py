"""K8s cluster tools — kubernetes_asyncio only; Pydantic args qua @register_tool."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Any, Literal

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from workers.k8s_tools import _load_k8s_config
from workers.tool_registry import register_tool

# ---------------------------------------------------------------------------
# Executor SA name used by the least-privilege RBAC tool.
# Overrideable via the args; this is the safe default.
# ---------------------------------------------------------------------------
_EXECUTOR_SA = "omni-worker"
_EXECUTOR_NAMESPACE = "multi-agent"
_CLUSTER_ADMIN_BINDING = "omni-worker-cluster-admin"

logger = logging.getLogger(__name__)


def _meta_readonly(*, required_evidence: list[str] | None = None, followup_tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "capability": "readonly",
        "side_effect": "none",
        "requires_readonly_before_mutate": False,
        "required_evidence": list(required_evidence or []),
        "followup_readonly_tools": list(followup_tools or []),
    }


def _meta_mutate(
    *,
    required_fields: list[str],
    required_evidence: list[str] | None = None,
    followup_tools: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "capability": "mutate",
        "side_effect": "cluster_state_change",
        "requires_readonly_before_mutate": True,
        "required_fields": list(required_fields),
        "required_evidence": list(required_evidence or []),
        "followup_readonly_tools": list(followup_tools or []),
    }


class ScaleDeploymentArgs(BaseModel):
    """Guardrail P0: replicas chỉ 0..10 tại tầng Pydantic."""

    name: str = Field(..., min_length=1, description="Deployment name")
    namespace: str = Field(..., min_length=1)
    replicas: int = Field(ge=0, description="Số replica (không giới hạn trên — RBAC cluster quyết định).")
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_scale_deployment",
    ScaleDeploymentArgs,
    metadata=_meta_mutate(
        required_fields=["name", "namespace", "replicas"],
        required_evidence=["target_workload_identity"],
        followup_tools=["k8s_get_deployment_state", "k8s_verify_rollout"],
    ),
)
async def tool_k8s_scale_deployment(ctx: Any, args: ScaleDeploymentArgs) -> str:
    try:
        await _load_k8s_config()
        apps = client.AppsV1Api()
        try:
            dep = await apps.read_namespaced_deployment(args.name, args.namespace)
            dep.spec.replicas = args.replicas
            await apps.replace_namespaced_deployment(args.name, args.namespace, dep)
            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            return (
                f"[DATA] scale_ok deployment={args.name} ns={args.namespace} replicas={args.replicas}\n"
                f"[DIAGNOSIS] Patched spec.replicas.{tail}"
            )
        finally:
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class DescribeResourceArgs(BaseModel):
    resource_type: Literal["Pod", "Deployment", "Service", "ConfigMap", "Secret"] = Field(
        ..., description="K8s kind: Pod | Deployment | Service | ConfigMap | Secret"
    )
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)


@register_tool(
    "k8s_describe_resource",
    DescribeResourceArgs,
    metadata=_meta_readonly(
        followup_tools=["k8s_get_events", "k8s_tail_logs", "k8s_patch_configmap", "k8s_patch_secret"],
    ),
)
async def tool_k8s_describe_resource(ctx: Any, args: DescribeResourceArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        try:
            body: dict[str, Any] = {}
            if args.resource_type == "Pod":
                obj = await v1.read_namespaced_pod(args.name, args.namespace)
                body = obj.to_dict()
            elif args.resource_type == "Deployment":
                obj = await apps.read_namespaced_deployment(args.name, args.namespace)
                body = obj.to_dict()
            elif args.resource_type == "ConfigMap":
                obj = await v1.read_namespaced_config_map(args.name, args.namespace)
                body = obj.to_dict()
            elif args.resource_type == "Secret":
                obj = await v1.read_namespaced_secret(args.name, args.namespace)
                body = obj.to_dict()
                # Strip Secret data values from the snippet — log only key names, not base64-encoded values.
                raw_data = body.get("data") or {}
                body["data"] = {k: "<redacted>" for k in raw_data}
                raw_string_data = body.get("string_data") or {}
                body["string_data"] = {k: "<redacted>" for k in raw_string_data}
                meta = body.get("metadata") or {}
                anns = meta.get("annotations") or {}
                if isinstance(anns, dict) and anns:
                    meta["annotations"] = {str(k): "<redacted>" for k in anns}
                    body["metadata"] = meta
            else:
                obj = await v1.read_namespaced_service(args.name, args.namespace)
                body = obj.to_dict()

            # Events use involvedObject.name field selector — works for all kinds.
            # For ConfigMap/Secret, events are sparse (kubelet mount events reference the
            # *Pod* not the ConfigMap/Secret), so ev_n=0 is normal and not an error.
            ev = await v1.list_namespaced_event(
                args.namespace,
                field_selector=f"involvedObject.name={args.name}",
            )
            ev_n = len(ev.items or [])
            snippet = json.dumps(body, default=str, ensure_ascii=False)[:2000]
            return (
                f"[DATA] describe_ok kind={args.resource_type} name={args.name} ns={args.namespace} events_n={ev_n}\n"
                f"[DIAGNOSIS] Object snippet:\n{snippet}"
            )
        finally:
            await v1.api_client.close()
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class TailLogsArgs(BaseModel):
    pod_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    lines: int = Field(default=100, ge=1, le=500)


@register_tool("k8s_tail_logs", TailLogsArgs, metadata=_meta_readonly())
async def tool_k8s_tail_logs(ctx: Any, args: TailLogsArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            log = await v1.read_namespaced_pod_log(
                args.pod_name,
                args.namespace,
                tail_lines=args.lines,
            )
            s = log or ""
            return (
                f"[DATA] logs_ok pod={args.pod_name} ns={args.namespace} lines={args.lines}\n"
                f"[DIAGNOSIS]\n{s[:8000]}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class CheckEndpointsArgs(BaseModel):
    service_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)


@register_tool("k8s_check_endpoints", CheckEndpointsArgs, metadata=_meta_readonly())
async def tool_k8s_check_endpoints(ctx: Any, args: CheckEndpointsArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            ep = await v1.read_namespaced_endpoints(args.service_name, args.namespace)
            subsets = ep.subsets or []
            ready = 0
            not_ready = 0
            for sub in subsets:
                for a in sub.addresses or []:
                    ready += 1
                for a in sub.not_ready_addresses or []:
                    not_ready += 1
            return (
                f"[DATA] endpoints_ok service={args.service_name} ns={args.namespace} "
                f"ready_addrs={ready} not_ready={not_ready} subsets={len(subsets)}\n"
                "[DIAGNOSIS] Endpoint slice summary (traffic readiness proxy)."
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class PatchResourceArgs(BaseModel):
    resource_type: Literal["Deployment"] = "Deployment"
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    patch_json: str = Field(..., min_length=2, description="JSON merge patch body")


@register_tool(
    "k8s_patch_resource",
    PatchResourceArgs,
    metadata=_meta_mutate(
        required_fields=["resource_type", "name", "namespace", "patch_json"],
        required_evidence=["patch_target_confirmed"],
        followup_tools=["k8s_get_deployment_state", "k8s_verify_rollout"],
    ),
)
async def tool_k8s_patch_resource(ctx: Any, args: PatchResourceArgs) -> str:
    """P1: destructive — minimal strategic merge patch (requires cluster RBAC)."""
    try:
        await _load_k8s_config()
        apps = client.AppsV1Api()
        try:
            patch_obj = json.loads(args.patch_json)
            if not isinstance(patch_obj, dict):
                return "[DATA] error\n[DIAGNOSIS] patch_json must be a JSON object."
            await apps.patch_namespaced_deployment(
                args.name,
                args.namespace,
                patch_obj,
                field_manager="omni-worker",
            )
            return (
                f"[DATA] patch_ok deployment={args.name} ns={args.namespace}\n"
                "[DIAGNOSIS] Strategic merge patch applied."
            )
        finally:
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


# ---------------------------------------------------------------------------
# k8s_patch_configmap — patch a single key in a ConfigMap (prod-safe)
# ---------------------------------------------------------------------------

class PatchConfigMapArgs(BaseModel):
    name: str = Field(..., min_length=1, description="ConfigMap name")
    namespace: str = Field(..., min_length=1)
    key: str = Field(..., min_length=1, description="Data key to set")
    value: str = Field(..., description="New value for the key")
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_patch_configmap",
    PatchConfigMapArgs,
    metadata=_meta_mutate(
        required_fields=["name", "namespace", "key", "value"],
        required_evidence=["configmap_exists", "config_key_confirmed"],
        followup_tools=["k8s_describe_resource", "k8s_verify_rollout"],
    ),
)
async def tool_k8s_patch_configmap(ctx: Any, args: PatchConfigMapArgs) -> str:
    """Patch a single key in a ConfigMap via strategic merge patch."""
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            patch = {"data": {args.key: args.value}}
            await v1.patch_namespaced_config_map(
                args.name,
                args.namespace,
                patch,
                field_manager="omni-worker",
            )
            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            return (
                f"[DATA] configmap_patch_ok name={args.name} ns={args.namespace} key={args.key}\n"
                f"[DIAGNOSIS] ConfigMap key patched to new value.{tail}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


# ---------------------------------------------------------------------------
# k8s_apply_rbac_least_privilege
# Creates a scoped Role + RoleBinding for the executor SA and removes the
# cluster-admin ClusterRoleBinding.  Safe to call idempotently.
# ---------------------------------------------------------------------------

class ApplyRbacLeastPrivilegeArgs(BaseModel):
    executor_sa: str = Field(default=_EXECUTOR_SA, min_length=1)
    namespace: str = Field(default=_EXECUTOR_NAMESPACE, min_length=1)
    remove_cluster_admin_binding: str = Field(
        default=_CLUSTER_ADMIN_BINDING,
        description="ClusterRoleBinding name to remove (empty = skip removal)",
    )
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_apply_rbac_least_privilege",
    ApplyRbacLeastPrivilegeArgs,
    metadata=_meta_mutate(
        required_fields=["executor_sa", "namespace"],
        required_evidence=["rbac_drift_signal"],
        followup_tools=["k8s_describe_resource"],
    ),
)
async def tool_k8s_apply_rbac_least_privilege(ctx: Any, args: ApplyRbacLeastPrivilegeArgs) -> str:
    """
    Harden executor RBAC:
    1. Create/replace a least-privilege Role scoped to args.namespace.
    2. Create/replace a RoleBinding attaching that Role to args.executor_sa.
    3. Delete the cluster-admin ClusterRoleBinding if present.
    """
    try:
        await _load_k8s_config()
        rbac = client.RbacAuthorizationV1Api()
        results: list[str] = []

        role_name = f"{args.executor_sa}-least-privilege"
        role_body = client.V1Role(
            metadata=client.V1ObjectMeta(name=role_name, namespace=args.namespace),
            rules=[
                client.V1PolicyRule(
                    api_groups=["apps"],
                    resources=["deployments"],
                    verbs=["get", "list", "patch", "update"],
                ),
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["pods"],
                    verbs=["get", "list", "delete"],
                ),
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["pods/log"],
                    verbs=["get"],
                ),
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["configmaps"],
                    verbs=["get", "create", "patch", "update"],
                ),
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["secrets"],
                    verbs=["get", "patch", "update"],
                ),
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["events"],
                    verbs=["get", "list"],
                ),
            ],
        )

        rb_body = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(name=f"{args.executor_sa}-binding", namespace=args.namespace),
            role_ref=client.V1RoleRef(
                api_group="rbac.authorization.k8s.io",
                kind="Role",
                name=role_name,
            ),
            subjects=[
                client.RbacV1Subject(
                    kind="ServiceAccount",
                    name=args.executor_sa,
                    namespace=args.namespace,
                )
            ],
        )

        try:
            try:
                await rbac.create_namespaced_role(args.namespace, role_body)
                results.append(f"role_created={role_name}")
            except ApiException as e:
                if e.status == 409:
                    # API signature is (name, namespace, body) — not (namespace, name, body).
                    await rbac.replace_namespaced_role(role_name, args.namespace, role_body)
                    results.append(f"role_replaced={role_name}")
                else:
                    raise

            try:
                await rbac.create_namespaced_role_binding(args.namespace, rb_body)
                results.append(f"rolebinding_created={args.executor_sa}-binding")
            except ApiException as e:
                if e.status == 409:
                    await rbac.replace_namespaced_role_binding(
                        f"{args.executor_sa}-binding", args.namespace, rb_body
                    )
                    results.append(f"rolebinding_replaced={args.executor_sa}-binding")
                else:
                    raise

            if args.remove_cluster_admin_binding:
                try:
                    await rbac.delete_cluster_role_binding(args.remove_cluster_admin_binding)
                    results.append(f"cluster_admin_binding_removed={args.remove_cluster_admin_binding}")
                except ApiException as e:
                    if e.status == 404:
                        results.append(
                            f"cluster_admin_binding_not_found={args.remove_cluster_admin_binding}"
                        )
                    else:
                        raise

            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            return (
                f"[DATA] rbac_hardened {' '.join(results)}\n"
                f"[DIAGNOSIS] Executor SA {args.executor_sa} now has least-privilege Role in {args.namespace}. "
                f"cluster-admin binding removed.{tail}"
            )
        finally:
            await rbac.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


# ---------------------------------------------------------------------------
# k8s_rollout_restart — restart a Deployment via pod-template annotation patch.
# This is the canonical "no-downtime restart" used by `kubectl rollout restart`.
# ---------------------------------------------------------------------------

class RolloutRestartArgs(BaseModel):
    deployment: str = Field(..., min_length=1, description="Deployment name")
    namespace: str = Field(..., min_length=1)
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_rollout_restart",
    RolloutRestartArgs,
    metadata=_meta_mutate(
        required_fields=["namespace", "deployment"],
        required_evidence=["workload_fault_confirmed"],
        followup_tools=["k8s_verify_rollout", "k8s_list_workload_pods"],
    ),
)
async def tool_k8s_rollout_restart(ctx: Any, args: RolloutRestartArgs) -> str:
    """
    Restart a Deployment by patching spec.template.metadata.annotations with a
    ``kubectl.kubernetes.io/restartedAt`` timestamp — identical to kubectl rollout restart.
    Safe for replicas > 1; triggers a rolling update without changing the pod spec.
    """
    try:
        await _load_k8s_config()
        apps = client.AppsV1Api()
        try:
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat() + "Z"
                            }
                        }
                    }
                }
            }
            await apps.patch_namespaced_deployment(
                args.deployment, args.namespace, patch, field_manager="omni-executor"
            )
            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            return (
                f"[DATA] rollout_restart_ok deployment={args.deployment} ns={args.namespace}\n"
                f"[DIAGNOSIS] Rolling restart triggered via pod-template annotation.{tail}"
            )
        finally:
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


# ---------------------------------------------------------------------------
# k8s_create_or_patch_configmap — idempotent create-or-update for a ConfigMap key.
# Resolves CreateContainerConfigError faults where the referenced ConfigMap is absent.
# ---------------------------------------------------------------------------

class CreateOrPatchConfigMapArgs(BaseModel):
    name: str = Field(..., min_length=1, description="ConfigMap name")
    namespace: str = Field(..., min_length=1)
    key: str = Field(..., min_length=1, description="Data key to set")
    value: str = Field(..., description="Value for the key (may be empty)")
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_create_or_patch_configmap",
    CreateOrPatchConfigMapArgs,
    metadata=_meta_mutate(
        required_fields=["name", "namespace", "key", "value"],
        required_evidence=["configmap_absent_or_wrong"],
        followup_tools=["k8s_describe_resource", "k8s_verify_rollout"],
    ),
)
async def tool_k8s_create_or_patch_configmap(ctx: Any, args: CreateOrPatchConfigMapArgs) -> str:
    """
    Create the ConfigMap if it does not exist; patch the specified key if it does.
    Semantics: create (201) → success; conflict (409) → strategic-merge patch the key.

    This enables autonomous resolution of CreateContainerConfigError faults without
    requiring the ConfigMap to be present before the incident.
    """
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            body = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=args.name, namespace=args.namespace),
                data={args.key: args.value},
            )
            try:
                await v1.create_namespaced_config_map(args.namespace, body)
                verb = "created"
            except ApiException as e:
                if e.status == 409:  # Already exists — patch the single key only.
                    await v1.patch_namespaced_config_map(
                        args.name,
                        args.namespace,
                        {"data": {args.key: args.value}},
                        field_manager="omni-executor",
                    )
                    verb = "patched"
                else:
                    raise
            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            return (
                f"[DATA] configmap_{verb} name={args.name} ns={args.namespace} key={args.key}\n"
                f"[DIAGNOSIS] ConfigMap {verb} autonomously (create-or-update).{tail}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


# ---------------------------------------------------------------------------
# Aliases + extra K8s surface (all errors → str for ReAct stability)
# ---------------------------------------------------------------------------

class GetLogsArgs(BaseModel):
    pod_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    container: str = Field(default="", description="Optional container name")
    lines: int = Field(default=500, ge=1, le=500)


@register_tool("k8s_get_logs", GetLogsArgs, metadata=_meta_readonly())
async def tool_k8s_get_logs(ctx: Any, args: GetLogsArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            kwargs: dict[str, Any] = {
                "name": args.pod_name,
                "namespace": args.namespace,
                "tail_lines": args.lines,
            }
            if (args.container or "").strip():
                kwargs["container"] = args.container.strip()
            log = await v1.read_namespaced_pod_log(**kwargs)
            s = log or ""
            return (
                f"[DATA] logs_ok pod={args.pod_name} ns={args.namespace} lines={args.lines}\n"
                f"[DIAGNOSIS]\n{s[:8000]}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class GetEventsArgs(BaseModel):
    namespace: str = Field(..., min_length=1)
    involved_name: str = Field(default="", description="involvedObject.name filter (e.g. pod name)")
    involved_kind: str = Field(default="Pod", description="involvedObject.kind")


@register_tool("k8s_get_events", GetEventsArgs, metadata=_meta_readonly())
async def tool_k8s_get_events(ctx: Any, args: GetEventsArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            fs = ""
            if (args.involved_name or "").strip():
                fs = (
                    f"involvedObject.name={args.involved_name.strip()},"
                    f"involvedObject.kind={args.involved_kind.strip() or 'Pod'}"
                )
            ev_kwargs: dict[str, Any] = {"namespace": args.namespace}
            if fs:
                ev_kwargs["field_selector"] = fs
            ev = await v1.list_namespaced_event(**ev_kwargs)
            lines: list[str] = []
            for it in (ev.items or [])[:80]:
                typ = getattr(it, "type", None) or ""
                reason = getattr(it, "reason", None) or ""
                msg = (getattr(it, "message", None) or "")[:400]
                lines.append(f"{typ} {reason}: {msg}")
            body = "\n".join(lines) if lines else "(no events in scope)"
            return f"[DATA] events_ok ns={args.namespace} n={len(lines)}\n[DIAGNOSIS]\n{body[:8000]}"
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class ListResourcesArgs(BaseModel):
    resource: Literal["pods", "deployments", "services", "configmaps", "secrets"] = Field(
        ..., description="Resource type to list"
    )
    namespace: str = Field(..., min_length=1)
    label_selector: str = Field(default="", description="Optional label selector")


@register_tool("k8s_list_resources", ListResourcesArgs, metadata=_meta_readonly())
async def tool_k8s_list_resources(ctx: Any, args: ListResourcesArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        try:
            ls = (args.label_selector or "").strip() or None
            names: list[str] = []
            if args.resource == "pods":
                lst = await v1.list_namespaced_pod(args.namespace, label_selector=ls)
                names = [p.metadata.name for p in (lst.items or []) if p.metadata and p.metadata.name]
            elif args.resource == "deployments":
                lst = await apps.list_namespaced_deployment(args.namespace, label_selector=ls)
                names = [d.metadata.name for d in (lst.items or []) if d.metadata and d.metadata.name]
            elif args.resource == "services":
                lst = await v1.list_namespaced_service(args.namespace, label_selector=ls)
                names = [s.metadata.name for s in (lst.items or []) if s.metadata and s.metadata.name]
            elif args.resource == "configmaps":
                lst = await v1.list_namespaced_config_map(args.namespace, label_selector=ls)
                names = [c.metadata.name for c in (lst.items or []) if c.metadata and c.metadata.name]
            else:
                lst = await v1.list_namespaced_secret(args.namespace, label_selector=ls)
                names = [s.metadata.name for s in (lst.items or []) if s.metadata and s.metadata.name]
            snippet = ", ".join(sorted(names)[:200])
            return (
                f"[DATA] list_ok kind={args.resource} ns={args.namespace} n={len(names)}\n"
                f"[DIAGNOSIS]\n{snippet}"
            )
        finally:
            await v1.api_client.close()
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class ScaleResourceArgs(BaseModel):
    name: str = Field(..., min_length=1, description="Deployment name")
    namespace: str = Field(..., min_length=1)
    replicas: int = Field(ge=0, description="Target replica count")
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_scale_resource",
    ScaleResourceArgs,
    metadata=_meta_mutate(
        required_fields=["name", "namespace", "replicas"],
        required_evidence=["target_workload_identity"],
        followup_tools=["k8s_get_deployment_state", "k8s_verify_rollout"],
    ),
)
async def tool_k8s_scale_resource(ctx: Any, args: ScaleResourceArgs) -> str:
    """Alias for scaling a Deployment (SDK)."""
    try:
        sd = ScaleDeploymentArgs(
            name=args.name,
            namespace=args.namespace,
            replicas=args.replicas,
            reasoning=args.reasoning,
        )
        return await tool_k8s_scale_deployment(ctx, sd)
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class DeletePodArgs(BaseModel):
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_delete_pod",
    DeletePodArgs,
    metadata=_meta_mutate(
        required_fields=["name", "namespace"],
        required_evidence=["pod_fault_confirmed"],
        followup_tools=["k8s_list_workload_pods", "k8s_get_events"],
    ),
)
async def tool_k8s_delete_pod(ctx: Any, args: DeletePodArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            await v1.delete_namespaced_pod(args.name, args.namespace)
            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            return (
                f"[DATA] delete_pod_ok name={args.name} ns={args.namespace}\n"
                f"[DIAGNOSIS] Pod delete requested.{tail}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


# ---------------------------------------------------------------------------
# get_resource_owner — OwnerReference traversal (Truth Layer)
# ---------------------------------------------------------------------------

async def get_resource_owner(pod_name: str, namespace: str) -> tuple[str, str] | None:
    """
    Recursively resolve the top-level controller that owns a Pod.

    Traversal chain (no naming heuristics — authoritative K8s metadata only):
      Pod.ownerReferences
        └── ReplicaSet → RS.ownerReferences → Deployment  (2 hops, most common)
        └── StatefulSet  (direct, 1 hop)
        └── DaemonSet    (direct, 1 hop)
        └── Job          (direct, 1 hop)

    Returns:
        (kind, name) tuple of the top-level controller, or None when the Pod is
        standalone, the chain exceeds depth=5, or K8s is unreachable.

    This function never branches on workload semantics — it only follows
    OwnerReference metadata. The caller decides what to do with the result.
    """
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    apps = client.AppsV1Api()

    async def _follow(kind: str, name: str, depth: int) -> tuple[str, str] | None:
        """Recursively follow a single ownerReference link."""
        if depth > 5:
            logger.warning("get_resource_owner: max depth exceeded at kind=%s name=%s", kind, name)
            return None
        # Terminal top-level controllers — stop traversal.
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            return kind, name
        if kind == "ReplicaSet":
            try:
                rs = await apps.read_namespaced_replica_set(name, namespace)
                for ref in (rs.metadata.owner_references or []):
                    if ref.kind:
                        result = await _follow(ref.kind, ref.name, depth + 1)
                        if result:
                            return result
            except ApiException as e:
                logger.debug("get_resource_owner: RS %s/%s not found (%s)", name, namespace, e.status)
            return None
        # Unknown intermediate kind — cannot recurse further.
        return None

    try:
        pod = await v1.read_namespaced_pod(pod_name, namespace)
        for ref in (pod.metadata.owner_references or []):
            if ref.kind:
                result = await _follow(ref.kind, ref.name, depth=0)
                if result:
                    return result
        return None  # standalone Pod — no controller found
    except ApiException as e:
        logger.debug(
            "get_resource_owner: pod %s/%s not found (%s)", pod_name, namespace, e.status
        )
        return None
    finally:
        await v1.api_client.close()
        await apps.api_client.close()


class GetDeploymentStateArgs(BaseModel):
    deployment: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)


@register_tool("k8s_get_deployment_state", GetDeploymentStateArgs, metadata=_meta_readonly())
async def tool_k8s_get_deployment_state(ctx: Any, args: GetDeploymentStateArgs) -> str:
    try:
        await _load_k8s_config()
        apps = client.AppsV1Api()
        try:
            dep = await apps.read_namespaced_deployment(args.deployment, args.namespace)
            spec = dep.spec
            st = dep.status
            desired = int(spec.replicas or 0) if spec else 0
            ready = int(st.ready_replicas or 0) if st else 0
            available = int(st.available_replicas or 0) if st else 0
            unavailable = int(st.unavailable_replicas or 0) if st and st.unavailable_replicas else 0
            updated = int(st.updated_replicas or 0) if st and st.updated_replicas else 0
            observed = int(st.observed_generation or 0) if st and st.observed_generation else 0
            generation = int(dep.metadata.generation or 0) if dep.metadata else 0
            ok = (desired == 0) or (ready >= desired and unavailable == 0 and observed >= generation)
            return (
                f"[DATA] deployment_state_ok ns={args.namespace} deployment={args.deployment} healthy={str(ok).lower()}\n"
                f"[DIAGNOSIS] desired={desired} ready={ready} available={available} "
                f"updated={updated} unavailable={unavailable} observed_gen={observed} gen={generation}"
            )
        finally:
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class ListWorkloadPodsArgs(BaseModel):
    deployment: str = Field(..., min_length=1, description="Deployment name")
    namespace: str = Field(..., min_length=1)


@register_tool("k8s_list_workload_pods", ListWorkloadPodsArgs, metadata=_meta_readonly())
async def tool_k8s_list_workload_pods(ctx: Any, args: ListWorkloadPodsArgs) -> str:
    try:
        await _load_k8s_config()
        apps = client.AppsV1Api()
        v1 = client.CoreV1Api()
        try:
            dep = await apps.read_namespaced_deployment(args.deployment, args.namespace)
            ml = (dep.spec.selector.match_labels or {}) if dep.spec and dep.spec.selector else {}
            if not isinstance(ml, dict) or not ml:
                return (
                    f"[DATA] workload_pods_ok ns={args.namespace} deployment={args.deployment} n=0\n"
                    "[DIAGNOSIS] deployment has empty selector.matchLabels; cannot resolve managed pods."
                )
            selector = ",".join(f"{k}={v}" for k, v in sorted(ml.items()))
            pods = await v1.list_namespaced_pod(args.namespace, label_selector=selector)
            names = sorted(
                [
                    p.metadata.name
                    for p in (pods.items or [])
                    if p.metadata is not None and p.metadata.name
                ]
            )
            return (
                f"[DATA] workload_pods_ok ns={args.namespace} deployment={args.deployment} n={len(names)}\n"
                f"[DIAGNOSIS] selector={selector} pods={', '.join(names[:100])}"
            )
        finally:
            await apps.api_client.close()
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class GetPodSecretRefsArgs(BaseModel):
    pod_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    container: str | None = Field(default=None, description="Optional container name filter")


@register_tool(
    "k8s_get_pod_secret_refs",
    GetPodSecretRefsArgs,
    metadata=_meta_readonly(
        required_evidence=["target_workload_identity"],
        followup_tools=["k8s_get_secret_keys", "k8s_describe_resource", "k8s_patch_secret"],
    ),
)
async def tool_k8s_get_pod_secret_refs(ctx: Any, args: GetPodSecretRefsArgs) -> str:
    """Read-only: extract Secret refs from env/envFrom without exposing values."""
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            resolved_pod_name = args.pod_name
            try:
                pod = await v1.read_namespaced_pod(resolved_pod_name, args.namespace)
            except ApiException as e:
                if e.status != 404:
                    raise
                # Pod from alert may be stale after restart/rollout; resolve newest pod by inferred workload label.
                inferred_deploy = ""
                parts = [p for p in resolved_pod_name.split("-") if p]
                if len(parts) >= 3:
                    inferred_deploy = "-".join(parts[:-2])
                if not inferred_deploy:
                    raise
                pods = await v1.list_namespaced_pod(args.namespace, label_selector=f"app={inferred_deploy}")
                items = [p for p in (pods.items or []) if p.metadata and p.metadata.name]
                if not items:
                    raise
                items.sort(key=lambda x: str(x.metadata.creation_timestamp or ""))
                pod = items[-1]
                resolved_pod_name = str(pod.metadata.name or resolved_pod_name)
            refs: list[dict[str, Any]] = []
            containers = list((pod.spec.containers or []) if pod.spec else [])
            for c in containers:
                cname = str(c.name or "").strip()
                if args.container and cname and cname != args.container:
                    continue
                for env in c.env or []:
                    vf = env.value_from
                    if vf is None or vf.secret_key_ref is None:
                        continue
                    sr = vf.secret_key_ref
                    refs.append(
                        {
                            "container": cname,
                            "env_var": str(env.name or ""),
                            "secret_name": str(sr.name or ""),
                            "secret_key": str(sr.key or ""),
                            "optional": bool(sr.optional) if sr.optional is not None else False,
                            "source": "env.secretKeyRef",
                        }
                    )
                for env_from in c.env_from or []:
                    if env_from.secret_ref is None:
                        continue
                    sref = env_from.secret_ref
                    refs.append(
                        {
                            "container": cname,
                            "env_var": "*",
                            "secret_name": str(sref.name or ""),
                            "secret_key": "*",
                            "optional": bool(sref.optional) if sref.optional is not None else False,
                            "source": "envFrom.secretRef",
                        }
                    )
            refs = [r for r in refs if r.get("secret_name")]
            refs_json = json.dumps(refs[:60], ensure_ascii=False, separators=(",", ":"))
            return (
                f"[DATA] pod_secret_refs_ok pod={resolved_pod_name} ns={args.namespace} refs_n={len(refs)}\n"
                f"[DIAGNOSIS] secret_ref_confirmed refs={refs_json}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class GetSecretKeysArgs(BaseModel):
    name: str = Field(..., min_length=1, description="Secret name")
    namespace: str = Field(..., min_length=1)


@register_tool(
    "k8s_get_secret_keys",
    GetSecretKeysArgs,
    metadata=_meta_readonly(
        required_evidence=["secret_ref_confirmed"],
        followup_tools=["k8s_patch_secret", "k8s_describe_resource"],
    ),
)
async def tool_k8s_get_secret_keys(ctx: Any, args: GetSecretKeysArgs) -> str:
    """Read-only: list Secret key names only; never return values."""
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            sec = await v1.read_namespaced_secret(args.name, args.namespace)
            data_keys = sorted(list((sec.data or {}).keys()))
            str_keys = sorted(list((sec.string_data or {}).keys()))
            all_keys = sorted(set(data_keys + str_keys))
            keys_json = json.dumps(all_keys[:120], ensure_ascii=False, separators=(",", ":"))
            return (
                f"[DATA] secret_keys_ok name={args.name} ns={args.namespace} keys_n={len(all_keys)}\n"
                f"[DIAGNOSIS] secret_key_catalog keys={keys_json}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class PatchSecretArgs(BaseModel):
    name: str = Field(..., min_length=1, description="Secret name")
    namespace: str = Field(..., min_length=1)
    key: str = Field(..., min_length=1, description="Secret key to set")
    value: str = Field(..., description="Plain value; sent as stringData")
    value_source: str = Field(
        default="",
        description="Source of truth for value (runbook/lab_env/human_confirmed/etc).",
    )
    value_source_ref: str = Field(
        default="",
        description="Reference ticket/doc/id for provenance.",
    )
    reasoning: str = Field(default="", max_length=500)


@register_tool(
    "k8s_patch_secret",
    PatchSecretArgs,
    metadata=_meta_mutate(
        required_fields=["name", "namespace", "key", "value"],
        required_evidence=["secret_ref_confirmed", "credential_source_of_truth"],
        followup_tools=["k8s_describe_resource", "k8s_tail_logs"],
    ),
)
async def tool_k8s_patch_secret(ctx: Any, args: PatchSecretArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            patch = {"stringData": {args.key: args.value}}
            await v1.patch_namespaced_secret(
                args.name,
                args.namespace,
                patch,
                field_manager="omni-worker",
            )
            rs = (args.reasoning or "").strip()
            tail = f" reasoning={rs[:200]}" if rs else ""
            src = (args.value_source or "").strip()
            src_ref = (args.value_source_ref or "").strip()
            src_tail = ""
            if src:
                src_tail = f" value_source={src[:80]}"
            if src_ref:
                src_tail += f" value_source_ref={src_ref[:120]}"
            return (
                f"[DATA] secret_patch_ok name={args.name} ns={args.namespace} key={args.key}\n"
                f"[DIAGNOSIS] Secret key patched via stringData (value redacted).{tail}{src_tail}"
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


class VerifyRolloutArgs(BaseModel):
    deployment: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    timeout_sec: int = Field(default=90, ge=5, le=600)
    poll_sec: float = Field(default=2.0, ge=0.5, le=15.0)


@register_tool("k8s_verify_rollout", VerifyRolloutArgs, metadata=_meta_readonly())
async def tool_k8s_verify_rollout(ctx: Any, args: VerifyRolloutArgs) -> str:
    try:
        await _load_k8s_config()
        apps = client.AppsV1Api()
        try:
            elapsed = 0.0
            while elapsed <= float(args.timeout_sec):
                dep = await apps.read_namespaced_deployment(args.deployment, args.namespace)
                spec = dep.spec
                st = dep.status
                desired = int(spec.replicas or 0) if spec else 0
                ready = int(st.ready_replicas or 0) if st else 0
                unavailable = int(st.unavailable_replicas or 0) if st and st.unavailable_replicas else 0
                observed = int(st.observed_generation or 0) if st and st.observed_generation else 0
                generation = int(dep.metadata.generation or 0) if dep.metadata else 0
                if desired == 0 or (ready >= desired and unavailable == 0 and observed >= generation):
                    return (
                        f"[DATA] rollout_verify_ok ns={args.namespace} deployment={args.deployment} elapsed_sec={elapsed:.1f}\n"
                        f"[DIAGNOSIS] desired={desired} ready={ready} unavailable={unavailable} observed_gen={observed} gen={generation}"
                    )
                await asyncio.sleep(float(args.poll_sec))
                elapsed += float(args.poll_sec)
            return (
                f"[DATA] rollout_verify_timeout ns={args.namespace} deployment={args.deployment} timeout_sec={args.timeout_sec}\n"
                "[DIAGNOSIS] Deployment not healthy within timeout window."
            )
        finally:
            await apps.api_client.close()
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"
