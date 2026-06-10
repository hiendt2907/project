"""Pre-mutate snapshot + auto-rollback for FULL_AUTO executor (S1.2).

Safety:
- Snapshots are stored in Redis with TTL (default 3600s).
- Secret values are NEVER stored — k8s_patch_secret rollback notifies human only.
- Rollback is best-effort: if K8s API fails, log + emit escalation (do not crash).
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from kubernetes_asyncio import client  # type: ignore
except ImportError:  # test environments without k8s SDK
    client = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SNAPSHOT_KEY = "omni:rollback:snapshot:{trace_id}"
_DEFAULT_TTL = 3600

# Tools that need a snapshot. k8s_rollout_restart is idempotent → not included.
_SNAPSHOT_REQUIRED_TOOLS = frozenset(
    {
        "k8s_scale_deployment",
        "k8s_patch_configmap",
        "k8s_patch_resource",
        "k8s_create_or_patch_configmap",
    }
)

# Secret tool: captured key name only, never the value.
_SECRET_NOTIFY_ONLY_TOOLS = frozenset({"k8s_patch_secret"})


def snapshot_required(resolved_tool_name: str) -> bool:
    return resolved_tool_name in _SNAPSHOT_REQUIRED_TOOLS | _SECRET_NOTIFY_ONLY_TOOLS


async def capture_pre_mutate_snapshot(
    ctx: Any,
    resolved_tool_name: str,
    args: dict[str, Any],
    trace_id: str,
    ttl_sec: int = _DEFAULT_TTL,
) -> dict[str, Any] | None:
    """Capture current K8s resource state before mutation. Stores in Redis."""
    if not snapshot_required(resolved_tool_name):
        return None

    snap: dict[str, Any] = {
        "tool_name": resolved_tool_name,
        "args_keys": sorted(args.keys()),
        "namespace": str(args.get("namespace") or ""),
        # Target name captured AT SNAPSHOT TIME — rollback must not depend on a
        # ctx side-channel set by whoever happens to trigger it later.
        "name": str(args.get("name") or args.get("deployment") or args.get("pod") or "").strip(),
        "trace_id": trace_id,
    }

    try:
        snap.update(await _capture(resolved_tool_name, args))
    except Exception as e:
        logger.warning(
            "[%s] event=rollback_snapshot_fail tool=%s err=%s",
            trace_id, resolved_tool_name, e,
        )
        snap["capture_error"] = str(e)

    redis = getattr(ctx, "redis", None)
    if redis is not None:
        try:
            key = _SNAPSHOT_KEY.format(trace_id=trace_id)
            await redis.setex(key, ttl_sec, json.dumps(snap, ensure_ascii=False))
            logger.info(
                "[%s] event=rollback_snapshot_stored tool=%s key=%s",
                trace_id, resolved_tool_name, key,
            )
        except Exception as e:
            logger.warning("[%s] event=rollback_snapshot_redis_fail err=%s", trace_id, e)

    return snap


async def apply_rollback_from_snapshot(
    ctx: Any,
    trace_id: str,
) -> tuple[bool, str]:
    """Restore K8s resource to state captured before mutation.

    Returns (success, message).
    """
    redis = getattr(ctx, "redis", None)
    if redis is None:
        return False, "rollback_skip: no redis"

    key = _SNAPSHOT_KEY.format(trace_id=trace_id)
    try:
        raw = await redis.get(key)
    except Exception as e:
        return False, f"rollback_skip: redis_get_fail {e}"

    if not raw:
        return False, "rollback_skip: no_snapshot"

    try:
        snap = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:
        return False, f"rollback_skip: snapshot_parse_fail {e}"

    tool_name = str(snap.get("tool_name") or "")
    if not tool_name:
        return False, "rollback_skip: snapshot_missing_tool_name"

    if tool_name in _SECRET_NOTIFY_ONLY_TOOLS:
        msg = (
            f"rollback_notify_only: tool={tool_name} ns={snap.get('namespace')} "
            f"trace={trace_id} — secret values never stored; human must restore manually."
        )
        logger.warning("[%s] %s", trace_id, msg)
        return False, msg

    try:
        ok, msg = await _apply(ctx, tool_name, snap, trace_id)
    except Exception as e:
        logger.exception("[%s] event=rollback_apply_exception tool=%s", trace_id, tool_name)
        return False, f"rollback_exception: {e}"

    if ok:
        try:
            await redis.delete(key)
        except Exception:
            pass
        logger.info("[%s] event=rollback_applied tool=%s", trace_id, tool_name)

    return ok, msg


# ---------------------------------------------------------------------------
# Per-tool capture helpers
# ---------------------------------------------------------------------------

async def _capture(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return tool-specific snapshot fields (no secrets)."""
    from workers.k8s_tools import _load_k8s_config  # type: ignore

    await _load_k8s_config()
    ns = str(args.get("namespace") or "").strip()
    name = str(args.get("name") or "").strip()

    if tool_name == "k8s_scale_deployment":
        apps = client.AppsV1Api()
        try:
            dep = await apps.read_namespaced_deployment(name, ns)
            return {"prior_replicas": dep.spec.replicas}
        finally:
            await apps.api_client.close()

    if tool_name in ("k8s_patch_configmap", "k8s_create_or_patch_configmap"):
        v1 = client.CoreV1Api()
        key = str(args.get("key") or "")
        try:
            try:
                cm = await v1.read_namespaced_config_map(name, ns)
                data = cm.data or {}
                prior_value = data.get(key)
                return {
                    "configmap_existed": True,
                    "key": key,
                    "prior_value": prior_value,
                }
            except Exception as e:
                if "404" in str(e) or "Not Found" in str(e):
                    return {"configmap_existed": False, "key": key}
                raise
        finally:
            await v1.api_client.close()

    if tool_name == "k8s_patch_resource":
        # Generic: capture the full spec section that might change.
        resource_type = str(args.get("resource_type") or "Deployment")
        patch_json_str = str(args.get("patch_json") or "{}")
        try:
            patch_obj = json.loads(patch_json_str)
            patch_keys = list(patch_obj.keys()) if isinstance(patch_obj, dict) else []
        except Exception:
            patch_keys = []
        return {
            "resource_type": resource_type,
            "patch_keys_affected": patch_keys,
            "prior_spec_capture": "manual_restore_required",
        }

    if tool_name == "k8s_patch_secret":
        # Never store secret value.
        key = str(args.get("key") or "")
        return {
            "secret_key_name": key,
            "secret_values_not_stored": True,
        }

    return {}


# ---------------------------------------------------------------------------
# Per-tool apply helpers
# ---------------------------------------------------------------------------

async def _apply(
    ctx: Any,
    tool_name: str,
    snap: dict[str, Any],
    trace_id: str,
) -> tuple[bool, str]:
    from workers.k8s_tools import _load_k8s_config  # type: ignore

    await _load_k8s_config()
    ns = str(snap.get("namespace") or "").strip()
    # Primary: name stored in the snapshot at capture time. Legacy fallback:
    # ctx.rollback_target_name (kept one release for old snapshots still in Redis).
    name = str(snap.get("name") or "").strip() or str(getattr(ctx, "rollback_target_name", "") or "")

    if tool_name == "k8s_scale_deployment":
        prior = snap.get("prior_replicas")
        if prior is None:
            return False, "rollback_skip: no prior_replicas in snapshot"
        if not name or not ns:
            return False, "rollback_skip: missing name/ns for scale rollback"
        apps = client.AppsV1Api()
        try:
            dep = await apps.read_namespaced_deployment(name, ns)
            dep.spec.replicas = int(prior)
            await apps.replace_namespaced_deployment(name, ns, dep)
            return True, f"rollback_ok: restored replicas={prior} deployment={name} ns={ns}"
        finally:
            await apps.api_client.close()

    if tool_name == "k8s_patch_configmap":
        key = str(snap.get("key") or "")
        prior_value = snap.get("prior_value")
        if not name or not ns or not key:
            return False, "rollback_skip: missing name/ns/key for configmap rollback"
        if prior_value is None:
            return False, f"rollback_skip: prior_value was None (key {key!r} did not exist before patch)"
        v1 = client.CoreV1Api()
        try:
            await v1.patch_namespaced_config_map(
                name, ns,
                {"data": {key: str(prior_value)}},
                field_manager="omni-worker-rollback",
            )
            return True, f"rollback_ok: restored configmap={name} ns={ns} key={key}"
        finally:
            await v1.api_client.close()

    if tool_name == "k8s_create_or_patch_configmap":
        existed = snap.get("configmap_existed", True)
        key = str(snap.get("key") or "")
        prior_value = snap.get("prior_value")
        if not name or not ns:
            return False, "rollback_skip: missing name/ns for create_or_patch rollback"
        v1 = client.CoreV1Api()
        try:
            if not existed:
                # ConfigMap was created by the mutate — delete it.
                await v1.delete_namespaced_config_map(name, ns)
                return True, f"rollback_ok: deleted created configmap={name} ns={ns}"
            elif prior_value is not None:
                await v1.patch_namespaced_config_map(
                    name, ns,
                    {"data": {key: str(prior_value)}},
                    field_manager="omni-worker-rollback",
                )
                return True, f"rollback_ok: restored configmap={name} ns={ns} key={key}"
            else:
                return False, f"rollback_skip: key={key!r} had no prior value — cannot determine safe restore"
        finally:
            await v1.api_client.close()

    if tool_name == "k8s_patch_resource":
        return (
            False,
            f"rollback_notify_only: tool={tool_name} ns={ns} name={name} "
            "patch_resource rollback requires manual restore — see snapshot for patch_keys_affected.",
        )

    return False, f"rollback_skip: no rollback handler for tool={tool_name}"
