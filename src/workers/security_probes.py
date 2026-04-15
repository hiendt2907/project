"""Security drift probes — RBAC and ConfigMap checks for self-remediation.

These probes are called by the prober when a security-drift alert arrives
(OmniRbacClusterAdminViolation, OmniConfigMapGodModeProd).  They emit
ProbeRunRaw evidence that the analyst uses to plan EXECUTE_MUTATE actions.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from workers.diagnostic_evidence import ProbeRunRaw
from workers.k8s_tools import _load_k8s_config

logger = logging.getLogger(__name__)

_CLUSTER_ADMIN_BINDING = "omni-worker-cluster-admin"
_WORKER_SA = "omni-worker"
_OMNI_NAMESPACE = "multi-agent"
_WORKER_CONFIGMAP = "omni-worker-config"


async def probe_k8s_rbac_drift(ctx: Any, ev: Any) -> ProbeRunRaw:
    """Detect whether the omni-worker ClusterRoleBinding grants cluster-admin.

    Returns FAILED when the binding exists (drift confirmed), PASSED when clean.
    """
    await _load_k8s_config()
    rbac = client.RbacAuthorizationV1Api()
    try:
        binding = await rbac.read_cluster_role_binding(_CLUSTER_ADMIN_BINDING)
        role_ref = getattr(binding, "role_ref", None)
        role_name = getattr(role_ref, "name", "") if role_ref else ""
        subjects = getattr(binding, "subjects", None) or []
        sa_names = [getattr(s, "name", "") for s in subjects]
        executor_sa = next((n for n in sa_names if n), _WORKER_SA)

        if role_name == "cluster-admin":
            return ProbeRunRaw(
                probe_name="rbac_drift",
                status="FAILED",
                raw_text=(
                    f"ClusterRoleBinding '{_CLUSTER_ADMIN_BINDING}' grants cluster-admin "
                    f"to service accounts: {sa_names}. "
                    "Bound workload SA must be least-privileged — Zero-Trust violation."
                ),
                structured_hint={
                    "binding_name": _CLUSTER_ADMIN_BINDING,
                    "role_ref": role_name,
                    "subjects": sa_names,
                    "drift_type": "cluster_admin_granted",
                    "recommended_tool": "k8s_apply_rbac_least_privilege",
                    "executor_sa": executor_sa,
                    "namespace": _OMNI_NAMESPACE,
                    "remove_cluster_admin_binding": _CLUSTER_ADMIN_BINDING,
                },
            )
        return ProbeRunRaw(
            probe_name="rbac_drift",
            status="PASSED",
            raw_text=f"ClusterRoleBinding '{_CLUSTER_ADMIN_BINDING}' role_ref={role_name!r} — no cluster-admin.",
            structured_hint={"binding_name": _CLUSTER_ADMIN_BINDING, "role_ref": role_name},
        )
    except ApiException as e:
        if e.status == 404:
            return ProbeRunRaw(
                probe_name="rbac_drift",
                status="PASSED",
                raw_text=f"ClusterRoleBinding '{_CLUSTER_ADMIN_BINDING}' not found — already clean.",
                structured_hint={"binding_name": _CLUSTER_ADMIN_BINDING, "not_found": True},
            )
        return ProbeRunRaw(
            probe_name="rbac_drift",
            status="INCONCLUSIVE",
            raw_text=f"API error ({e.status}): {e.reason}",
            structured_hint={"api_status": e.status},
        )
    finally:
        await rbac.api_client.close()


async def probe_k8s_configmap_security_drift(ctx: Any, ev: Any) -> ProbeRunRaw:
    """Detect OMNI_GOD_MODE=true when OMNI_ENV_MODE=prod in the worker ConfigMap.

    Returns FAILED when both conditions are true (drift confirmed), PASSED when clean.
    """
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    try:
        cm = await v1.read_namespaced_config_map(_WORKER_CONFIGMAP, _OMNI_NAMESPACE)
        data: dict[str, str] = dict(cm.data or {})
        env_mode = data.get("OMNI_ENV_MODE", "").strip().lower()
        god_mode = data.get("OMNI_GOD_MODE", "").strip().lower()

        if env_mode == "prod" and god_mode == "true":
            return ProbeRunRaw(
                probe_name="configmap_security_drift",
                status="FAILED",
                raw_text=(
                    f"ConfigMap '{_WORKER_CONFIGMAP}' has OMNI_ENV_MODE=prod "
                    f"and OMNI_GOD_MODE=true simultaneously. "
                    "This violates the prod least-privilege invariant."
                ),
                structured_hint={
                    "configmap_name": _WORKER_CONFIGMAP,
                    "namespace": _OMNI_NAMESPACE,
                    "OMNI_ENV_MODE": env_mode,
                    "OMNI_GOD_MODE": god_mode,
                    "drift_type": "god_mode_in_prod",
                    "recommended_tool": "k8s_patch_configmap",
                    "patch_key": "OMNI_GOD_MODE",
                    "patch_value": "false",
                },
            )
        return ProbeRunRaw(
            probe_name="configmap_security_drift",
            status="PASSED",
            raw_text=(
                f"ConfigMap '{_WORKER_CONFIGMAP}' OMNI_ENV_MODE={env_mode!r} "
                f"OMNI_GOD_MODE={god_mode!r} — no drift."
            ),
            structured_hint={
                "configmap_name": _WORKER_CONFIGMAP,
                "OMNI_ENV_MODE": env_mode,
                "OMNI_GOD_MODE": god_mode,
            },
        )
    except ApiException as e:
        return ProbeRunRaw(
            probe_name="configmap_security_drift",
            status="INCONCLUSIVE",
            raw_text=f"API error ({e.status}): {e.reason}",
            structured_hint={"api_status": e.status},
        )
    finally:
        await v1.api_client.close()
