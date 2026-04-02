"""Audit Prometheus + LGTM (Loki/Grafana/Tempo) — httpx + kubernetes_asyncio."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

from workers.settings import default_prometheus_http_base

logger = logging.getLogger(__name__)

LGTM_DEPLOYMENTS = ("loki", "grafana")


def _monitor_stack_namespace(ctx: Any) -> str:
    s = getattr(ctx, "settings", None)
    if s is not None:
        v = getattr(s, "monitor_stack_namespace", None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "monitor"


async def _kube_load() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


def _prometheus_base(ctx: Any) -> str:
    s = getattr(ctx, "settings", None)
    if s is not None:
        u = getattr(s, "prometheus_url", None)
        if isinstance(u, str) and u.strip():
            return u.strip().rstrip("/")
    return default_prometheus_http_base()


def _prometheus_targets_base(ctx: Any) -> str:
    s = getattr(ctx, "settings", None)
    if s is not None:
        u = getattr(s, "vmagent_url", None)
        if isinstance(u, str) and u.strip():
            return u.strip().rstrip("/")
    return default_prometheus_http_base()


async def tool_audit_observability_stack(ctx: Any, args: dict[str, Any]) -> str:
    """
    Health Prometheus, targets scrape, log Prometheus (disk), readiness LGTM.
    args: pod_name?, namespace?, monitor_namespace? (default monitor)
    """
    pod_hint = str(args.get("pod_name") or args.get("pod") or "").strip()
    ns_hint = str(args.get("namespace") or "").strip()
    mon_ns = str(args.get("monitor_namespace") or _monitor_stack_namespace(ctx)).strip() or _monitor_stack_namespace(ctx)

    lines: list[str] = ["[DATA] audit_observability_stack"]
    pm_url = _prometheus_base(ctx)
    ta_url = _prometheus_targets_base(ctx)
    lines.append(f"effective_prometheus_url={pm_url}")
    lines.append(f"effective_targets_url={ta_url}")

    async with httpx.AsyncClient(timeout=25.0) as hc:
        try:
            hr = await hc.get(f"{pm_url}/-/healthy")
            lines.append(f"prometheus_healthy status={hr.status_code}")
            if hr.status_code >= 400:
                lines.append("[WARN] Prometheus /-/healthy không OK — kiểm tra OMNI_PROMETHEUS_URL và Service.")
        except Exception as e:
            lines.append(f"[WARN] Prometheus connectivity: {e!s}")

        try:
            tr = await hc.get(f"{ta_url}/api/v1/targets")
            tr.raise_for_status()
            tj = tr.json()
            data = tj.get("data") or tj
            active = data.get("activeTargets") or data.get("active_targets") or []
            up_labels: list[dict[str, Any]] = []
            for t in active[:500]:
                lab = t.get("labels") or {}
                hs = str(t.get("health", "")).lower()
                if hs == "up":
                    up_labels.append(lab)
            lines.append(f"prometheus activeTargets~={len(active)} up~={len(up_labels)}")
            if pod_hint:
                found = False
                blob = json.dumps(active, ensure_ascii=False)[:200_000]
                if pod_hint in blob:
                    found = True
                for lab in up_labels:
                    for _k, v in (lab or {}).items():
                        if pod_hint in str(v):
                            found = True
                            break
                if not found:
                    lines.append(
                        f"[WARN] Pod `{pod_hint}` có trong K8s nhưng **không** thấy trong Prometheus targets UP. "
                        f"ns_user={ns_hint or '-'}. "
                        "Nguyên nhân thường gặp: Pod **không** có annotation prometheus.io/scrape=true + port, "
                        "hoặc **không** expose /metrics."
                    )
        except Exception as e:
            lines.append(f"[WARN] prometheus /api/v1/targets: {e!s}")

    try:
        await _kube_load()
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        try:
            pl = await v1.list_namespaced_pod(namespace=mon_ns, label_selector="app=prometheus")
            names = [p.metadata.name for p in (pl.items or []) if p.metadata and p.metadata.name]
            if not names:
                pl2 = await v1.list_namespaced_pod(namespace=mon_ns)
                names = [
                    p.metadata.name
                    for p in (pl2.items or [])
                    if p.metadata and p.metadata.name and str(p.metadata.name).startswith("prometheus")
                ]
            if names:
                pname = names[0]
                try:
                    raw = await v1.read_namespaced_pod_log(
                        name=pname,
                        namespace=mon_ns,
                        tail_lines=120,
                        container="prometheus",
                    )
                    low = (raw or "").lower()
                    if "no space left on device" in low or "write error" in low:
                        lines.append(
                            "[WARN] Log Prometheus có dấu hiệu disk full / write error — kiểm tra PVC/storage."
                        )
                    else:
                        lines.append("prometheus_logs_tail: no disk-full pattern in last 120 lines")
                except ApiException as e:
                    lines.append(f"[WARN] read prometheus log: {e.status} {e.reason}")
            else:
                lines.append(f"[WARN] Không tìm thấy pod prometheus trong ns={mon_ns}")

            for dep in LGTM_DEPLOYMENTS:
                try:
                    d = await apps.read_namespaced_deployment(name=dep, namespace=mon_ns)
                    spec_r = d.spec.replicas or 0
                    ready = d.status.ready_replicas or 0
                    if spec_r and ready < spec_r:
                        lines.append(f"[WARN] Deployment `{dep}` chưa Ready ({ready}/{spec_r}) — LGTM stack lệch.")
                    else:
                        lines.append(f"lgtm `{dep}` ready={ready}/{spec_r or '?'}")
                except ApiException as e:
                    if e.status == 404:
                        lines.append(f"lgtm `{dep}`: not found in {mon_ns}")
                    else:
                        lines.append(f"[WARN] deployment `{dep}`: {e.status}")
            try:
                st = await apps.read_namespaced_stateful_set(name="prometheus", namespace=mon_ns)
                spec_r = st.spec.replicas or 0
                ready = st.status.ready_replicas or 0
                if spec_r and ready < spec_r:
                    lines.append(
                        f"[WARN] StatefulSet `prometheus` chưa Ready ({ready}/{spec_r}) — metrics stack lệch."
                    )
                else:
                    lines.append(f"lgtm prometheus (StatefulSet) ready={ready}/{spec_r or '?'}")
            except ApiException as e:
                if e.status == 404:
                    lines.append(f"lgtm prometheus: not found in {mon_ns}")
                else:
                    lines.append(f"[WARN] statefulset prometheus: {e.status}")
        finally:
            await v1.api_client.close()
            await apps.api_client.close()
    except Exception as e:
        lines.append(f"[WARN] k8s audit: {e!s}")

    diag = (
        "Chẩn đoán: (1) So khớp OMNI_PROMETHEUS_URL / OMNI_VMAGENT_URL (cùng Prometheus :9090). "
        "(2) Series rỗng → thiếu scrape hoặc label không khớp PromQL. "
        "(3) Pod không trong targets UP → thiếu exporter /metrics hoặc annotation prometheus.io/*."
    )
    return "\n".join(lines) + f"\n[DIAGNOSIS] {diag}"
