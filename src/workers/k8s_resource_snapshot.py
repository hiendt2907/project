"""Read small JSON snapshot of a namespaced object (Kubernetes async SDK)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from workers.k8s_tools import _load_k8s_config

logger = logging.getLogger(__name__)

_MAX_JSON_CHARS = 14_000


def _clip_obj(d: dict[str, Any]) -> dict[str, Any]:
    md = d.get("metadata") or {}
    st = d.get("status")
    out: dict[str, Any] = {
        "apiVersion": d.get("apiVersion"),
        "kind": d.get("kind"),
        "resourceVersion": (md or {}).get("resourceVersion") if isinstance(md, dict) else None,
        "metadata": {
            "name": (md or {}).get("name") if isinstance(md, dict) else None,
            "namespace": (md or {}).get("namespace") if isinstance(md, dict) else None,
            "uid": (md or {}).get("uid") if isinstance(md, dict) else None,
        },
    }
    if st is not None:
        out["status"] = st
    return out


async def fetch_last_known_state(
    namespace: str,
    kind: str,
    name: str,
    *,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    """
    Return a small dict for audit/DLQ: resourceVersion, metadata, status.
    On failure: { "unavailable": True, "reason": "..." }.
    """
    kn = (kind or "").strip()
    ns = (namespace or "").strip()
    nm = (name or "").strip()
    if not kn or not nm:
        return {"unavailable": True, "reason": "missing_kind_or_name"}

    async def _read() -> dict[str, Any]:
        await _load_k8s_config()
        kn_lower = kn.lower()

        if kn_lower == "namespace":
            v1 = client.CoreV1Api()
            try:
                obj = await v1.read_namespace(nm)
                d = obj.to_dict()
                await v1.api_client.close()
                return _clip_obj(d)
            except ApiException as e:
                await v1.api_client.close()
                return {"unavailable": True, "reason": f"ApiException:{e.status}:{e.reason}"}
            except Exception as e:
                try:
                    await v1.api_client.close()
                except Exception:
                    pass
                return {"unavailable": True, "reason": type(e).__name__}

        if not ns:
            return {"unavailable": True, "reason": "missing_namespace_for_namespaced_kind"}

        if kn_lower == "deployment":
            apps = client.AppsV1Api()
            try:
                dep = await apps.read_namespaced_deployment(nm, ns)
                d = dep.to_dict()
                await apps.api_client.close()
                return _clip_obj(d)
            except ApiException as e:
                await apps.api_client.close()
                return {"unavailable": True, "reason": f"ApiException:{e.status}:{e.reason}"}
            except Exception as e:
                try:
                    await apps.api_client.close()
                except Exception:
                    pass
                return {"unavailable": True, "reason": type(e).__name__}

        if kn_lower == "pod":
            v1 = client.CoreV1Api()
            try:
                po = await v1.read_namespaced_pod(nm, ns)
                d = po.to_dict()
                await v1.api_client.close()
                return _clip_obj(d)
            except ApiException as e:
                await v1.api_client.close()
                return {"unavailable": True, "reason": f"ApiException:{e.status}:{e.reason}"}
            except Exception as e:
                try:
                    await v1.api_client.close()
                except Exception:
                    pass
                return {"unavailable": True, "reason": type(e).__name__}

        if kn_lower == "service":
            v1 = client.CoreV1Api()
            try:
                sv = await v1.read_namespaced_service(nm, ns)
                d = sv.to_dict()
                await v1.api_client.close()
                return _clip_obj(d)
            except ApiException as e:
                await v1.api_client.close()
                return {"unavailable": True, "reason": f"ApiException:{e.status}:{e.reason}"}
            except Exception as e:
                try:
                    await v1.api_client.close()
                except Exception:
                    pass
                return {"unavailable": True, "reason": type(e).__name__}

        return {"unavailable": True, "reason": f"unsupported_kind:{kn}"}

    try:
        raw = await asyncio.wait_for(_read(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return {"unavailable": True, "reason": "snapshot_timeout"}
    except Exception as e:
        logger.warning("fetch_last_known_state: %s", e)
        return {"unavailable": True, "reason": type(e).__name__}

    if raw.get("unavailable"):
        return raw
    s = json.dumps(raw, default=str, ensure_ascii=False)
    if len(s) > _MAX_JSON_CHARS:
        return {"truncated": True, "resourceVersion": raw.get("resourceVersion"), "note": "json_truncated"}
    return raw
