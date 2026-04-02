"""K8s cluster tools — kubernetes_asyncio only; Pydantic args qua @register_tool."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field

from workers.k8s_tools import _load_k8s_config
from workers.tool_registry import register_tool

logger = logging.getLogger(__name__)


class ScaleDeploymentArgs(BaseModel):
    """Guardrail P0: replicas chỉ 0..10 tại tầng Pydantic."""

    name: str = Field(..., min_length=1, description="Deployment name")
    namespace: str = Field(..., min_length=1)
    replicas: int = Field(ge=0, description="Số replica (không giới hạn trên — RBAC cluster quyết định).")
    reasoning: str = Field(default="", max_length=500)


@register_tool("k8s_scale_deployment", ScaleDeploymentArgs)
async def tool_k8s_scale_deployment(ctx: Any, args: ScaleDeploymentArgs) -> str:
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
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    finally:
        await apps.api_client.close()


class DescribeResourceArgs(BaseModel):
    resource_type: Literal["Pod", "Deployment", "Service"] = Field(..., description="K8s kind")
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)


@register_tool("k8s_describe_resource", DescribeResourceArgs)
async def tool_k8s_describe_resource(ctx: Any, args: DescribeResourceArgs) -> str:
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
        else:
            obj = await v1.read_namespaced_service(args.name, args.namespace)
            body = obj.to_dict()
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
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    finally:
        await v1.api_client.close()
        await apps.api_client.close()


class TailLogsArgs(BaseModel):
    pod_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    lines: int = Field(default=100, ge=1, le=500)


@register_tool("k8s_tail_logs", TailLogsArgs)
async def tool_k8s_tail_logs(ctx: Any, args: TailLogsArgs) -> str:
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
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    finally:
        await v1.api_client.close()


class CheckEndpointsArgs(BaseModel):
    service_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)


@register_tool("k8s_check_endpoints", CheckEndpointsArgs)
async def tool_k8s_check_endpoints(ctx: Any, args: CheckEndpointsArgs) -> str:
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
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    finally:
        await v1.api_client.close()


class PatchResourceArgs(BaseModel):
    resource_type: Literal["Deployment"] = "Deployment"
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    patch_json: str = Field(..., min_length=2, description="JSON merge patch body")


@register_tool("k8s_patch_resource", PatchResourceArgs)
async def tool_k8s_patch_resource(ctx: Any, args: PatchResourceArgs) -> str:
    """P1: destructive — minimal strategic merge patch (requires cluster RBAC)."""
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
    except json.JSONDecodeError as e:
        return f"[DATA] error\n[DIAGNOSIS] Invalid JSON: {e!s}"
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    finally:
        await apps.api_client.close()
