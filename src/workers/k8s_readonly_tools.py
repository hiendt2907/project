"""
K8s Read-Only Tools V6.
Bao gồm: describe/list nodes, conditions, services, ingress.
Tất cả đều có cờ force_refresh: bool = Field(default=False)
"""
from typing import Any
import logging
from pydantic import BaseModel, Field
from kubernetes_asyncio import client

from workers.k8s_tools import _load_k8s_config
from workers.tool_registry import register_tool

logger = logging.getLogger(__name__)

class K8sNodeArgs(BaseModel):
    force_refresh: bool = Field(default=False, description="Đặt True nếu cần bỏ qua cache (vd: sau restart/patch)")
    node_name: str = Field(default="", description="Để trống nếu muốn list toàn bộ")

class K8sServiceArgs(BaseModel):
    force_refresh: bool = Field(default=False, description="Đặt True nếu cần bỏ qua cache")
    namespace: str = Field(default="", description="Namespace (để trống = toàn cluster)")

class K8sIngressArgs(BaseModel):
    force_refresh: bool = Field(default=False, description="Đặt True nếu cần bỏ qua cache")
    namespace: str = Field(default="", description="Namespace (để trống = toàn cluster)")

@register_tool("k8s_list_nodes", K8sNodeArgs)
async def tool_k8s_list_nodes(ctx: Any, args: K8sNodeArgs) -> str:
    """List hoặc Describe Node + Conditions."""
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        
        target = args.node_name.strip()
        if target:
            try:
                node = await v1.read_node(target)
                status = node.status.phase or "Unknown"
                conds = []
                for c in node.status.conditions or []:
                    conds.append(f"{c.type}={c.status} ({c.reason})")
                return f"[DATA] Node {target}: {status}\nConditions:\n" + "\n".join(conds) + "\n[DIAGNOSIS] Đã lấy trạng thái node."
            except Exception as e:
                return f"[DATA] api_error\n[DIAGNOSIS] K8s lôi khi read_node: {e}"
        else:
            resp = await v1.list_node()
            rows = []
            for n in resp.items:
                name = n.metadata.name
                rows.append(f"- {name}")
            return f"[DATA] Có {len(rows)} nodes:\n" + "\n".join(rows) + "\n[DIAGNOSIS] Đã list cluster nodes."
    except Exception as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Lỗi K8s: {e}"
    finally:
        await v1.api_client.close()

@register_tool("k8s_node_conditions", K8sNodeArgs)
async def tool_k8s_node_conditions(ctx: Any, args: K8sNodeArgs) -> str:
    """Tương tự k8s_list_nodes, focus vào conditions"""
    return await tool_k8s_list_nodes(ctx, args)

@register_tool("k8s_list_services", K8sServiceArgs)
async def tool_k8s_list_services(ctx: Any, args: K8sServiceArgs) -> str:
    try:
        await _load_k8s_config()
        v1 = client.CoreV1Api()
        if args.namespace:
            resp = await v1.list_namespaced_service(args.namespace)
        else:
            resp = await v1.list_service_for_all_namespaces()
            
        rows = [f"{s.metadata.namespace}/{s.metadata.name} type={s.spec.type} clusterIP={s.spec.cluster_ip}" for s in resp.items]
        return f"[DATA] Services:\n" + "\n".join(rows[:50]) + ("\n...(truncated)" if len(rows)>50 else "") + "\n[DIAGNOSIS] OK"
    except Exception as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Lỗi khi get services: {e}"
    finally:
        await v1.api_client.close()

@register_tool("k8s_list_ingress", K8sIngressArgs)
async def tool_k8s_list_ingress(ctx: Any, args: K8sIngressArgs) -> str:
    try:
        await _load_k8s_config()
        netv1 = client.NetworkingV1Api()
        if args.namespace:
            resp = await netv1.list_namespaced_ingress(args.namespace)
        else:
            resp = await netv1.list_ingress_for_all_namespaces()
            
        rows = [f"{i.metadata.namespace}/{i.metadata.name}" for i in resp.items]
        return f"[DATA] Ingresses:\n" + "\n".join(rows[:50]) + ("\n...(truncated)" if len(rows)>50 else "") + "\n[DIAGNOSIS] OK"
    except Exception as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Lỗi khi get ingress: {e}"
    finally:
        await netv1.api_client.close()
