#!/usr/bin/env python3
"""Resolve real pod (+uid, container) from Deployment for Prometheus-shaped test payloads."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _kube_base(root: Path) -> list[str]:
    return [str(root / "scripts" / "with_working_kube.sh")]


def _run_kube(root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    cmd = _kube_base(root) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()


def resolve_deployment_pod(
    root: Path,
    namespace: str,
    deployment: str,
    *,
    container_hint: str | None = None,
) -> dict[str, Any]:
    """Return pod name, namespace, container, uid for first Running pod of deployment."""
    rc, dep_json = _run_kube(root, "get", "deploy", deployment, "-n", namespace, "-o", "json")
    if rc != 0:
        raise RuntimeError(f"kubectl get deploy {deployment}: {dep_json[:500]}")
    dep = json.loads(dep_json)
    sel = (dep.get("spec") or {}).get("selector") or {}
    match = (sel.get("matchLabels") or {}) if isinstance(sel, dict) else {}
    if not match:
        raise RuntimeError(f"deployment {deployment} has no matchLabels")
    label_sel = ",".join(f"{k}={v}" for k, v in sorted(match.items()))
    rc2, pod_json = _run_kube(root, "get", "pods", "-n", namespace, "-l", label_sel, "-o", "json")
    if rc2 != 0:
        raise RuntimeError(f"kubectl get pods -l {label_sel}: {pod_json[:500]}")
    data = json.loads(pod_json)
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"no pods for deployment {deployment} in {namespace}")
    # Prefer Running
    picked = None
    for it in items:
        phase = str((it.get("status") or {}).get("phase") or "")
        if phase == "Running":
            picked = it
            break
    if picked is None:
        picked = items[0]
    meta = picked.get("metadata") or {}
    name = str(meta.get("name") or "")
    uid = str(meta.get("uid") or "")
    ns = str(meta.get("namespace") or namespace)
    spec = picked.get("spec") or {}
    containers = spec.get("containers") or []
    cname = container_hint or ""
    if not cname and containers:
        cname = str((containers[0] or {}).get("name") or "")
    return {"namespace": ns, "pod": name, "container": cname, "uid": uid}


def resolve_from_env(root: Path | None = None) -> dict[str, Any]:
    """CLI helper: OMNI_RESOLVE_DEPLOYMENT + OMNI_RESOLVE_NS + optional OMNI_RESOLVE_CONTAINER."""
    root = root or Path(__file__).resolve().parents[1]
    ns = os.environ.get("OMNI_RESOLVE_NS", "multi-agent")
    dep = os.environ.get("OMNI_RESOLVE_DEPLOYMENT", "").strip()
    if not dep:
        raise SystemExit("OMNI_RESOLVE_DEPLOYMENT required")
    ch = os.environ.get("OMNI_RESOLVE_CONTAINER") or None
    return resolve_deployment_pod(root, ns, dep, container_hint=ch)


if __name__ == "__main__":
    print(json.dumps(resolve_from_env(), ensure_ascii=False, indent=2))
