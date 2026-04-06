"""Khám lâm sàng trực tiếp qua K8s API (SDK-first) — real-time, không phụ thuộc Prometheus."""

from __future__ import annotations

import logging
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from workers.diagnostic_evidence import ProbeRunRaw
from workers.diagnostic_resource import pod_identity_from_event
from workers.k8s_tools import _load_k8s_config
from workers.proactive_models import AnomalyEvent
from workers.handlers import WorkerHandlerContext

logger = logging.getLogger(__name__)


def _pod_needs_log_tail(pod: Any) -> bool:
    """True nếu cần tail log (phase lệch, không Ready, OOM/Crash/waiting)."""
    st = pod.status
    phase = getattr(st, "phase", None) or ""
    if phase and phase != "Running":
        return True
    for c in getattr(st, "container_statuses", None) or []:
        if c.state and c.state.waiting and getattr(c.state.waiting, "reason", None):
            return True
        if c.state and c.state.terminated:
            tr = getattr(c.state.terminated, "reason", None) or ""
            if tr in ("OOMKilled", "Error", "ContainerStatusUnknown"):
                return True
    for cond in getattr(st, "conditions", None) or []:
        if getattr(cond, "type", None) == "Ready" and getattr(cond, "status", None) == "False":
            return True
    return False


def _pick_container_for_log(pod: Any) -> str | None:
    """Ưu tiên container đang waiting/terminated; không thì container đầu."""
    names: list[str] = []
    for c in getattr(pod.spec, "containers", None) or []:
        if c.name:
            names.append(c.name)
    for c in getattr(pod.status, "container_statuses", None) or []:
        if c.state and (c.state.waiting or c.state.terminated):
            if c.name:
                return c.name
    return names[0] if names else None


async def probe_k8s_clinical_pod_status(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """CoreV1Api: phase, conditions, container reasons (Evicted/OOM/CrashLoop…)."""
    ns, pod, _ = pod_identity_from_event(ev)
    if not ns or not pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_status",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    try:
        p = await v1.read_namespaced_pod(name=pod, namespace=ns)
        st = p.status
        phase = getattr(st, "phase", "") or "?"
        cond_bits: list[str] = []
        for c in getattr(st, "conditions", None) or []:
            cond_bits.append(
                f"{getattr(c, 'type', '?')}={getattr(c, 'status', '?')} "
                f"reason={getattr(c, 'reason', '') or '-'}"
            )
        ctr_bits: list[str] = []
        for c in getattr(st, "container_statuses", None) or []:
            nm = c.name or "?"
            if c.state and c.state.waiting:
                ctr_bits.append(f"{nm}:waiting={c.state.waiting.reason}")
            if c.state and c.state.terminated:
                ctr_bits.append(
                    f"{nm}:term={c.state.terminated.reason} exit={c.state.terminated.exit_code}"
                )
        raw = (
            f"phase={phase}\nconditions={'; '.join(cond_bits) or 'none'}\n"
            f"containers={'; '.join(ctr_bits) or 'none'}"
        )[:4000]
        structured: dict[str, Any] = {
            "source": "K8s_SDK",
            "kind": "PodStatus",
            "namespace": ns,
            "pod": pod,
            "phase": phase,
            "conditions": cond_bits[:20],
            "container_signals": ctr_bits[:20],
        }
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_status",
            status="PASSED",
            raw_text=raw,
            structured_hint=structured,
        )
    except Exception as e:
        logger.warning("k8s_clinical_pod_status: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_status",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500]},
        )
    finally:
        try:
            await v1.api_client.close()
        except Exception:
            pass


async def probe_k8s_clinical_pod_metrics(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """metrics.k8s.io/v1beta1 PodMetrics — CPU/Mem usage tại thời điểm gọi."""
    ns, pod, _ = pod_identity_from_event(ev)
    if not ns or not pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_metrics",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    custom = client.CustomObjectsApi()
    try:
        obj = await custom.get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=ns,
            plural="pods",
            name=pod,
        )
        if not isinstance(obj, dict):
            obj = dict(obj)  # type: ignore[arg-type]
        containers = obj.get("containers") or []
        lines: list[str] = []
        ctr_struct: list[dict[str, Any]] = []
        for c in containers:
            if not isinstance(c, dict):
                continue
            usage = c.get("usage") or {}
            nm = c.get("name", "?")
            cpu = usage.get("cpu")
            mem = usage.get("memory")
            lines.append(f"{nm}: cpu={cpu} memory={mem}")
            ctr_struct.append({"name": nm, "cpu": cpu, "memory": mem})
        raw = "\n".join(lines)[:4000] or "no containers in PodMetrics"
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_metrics",
            status="PASSED" if lines else "INCONCLUSIVE",
            raw_text=raw,
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodMetrics",
                "namespace": ns,
                "pod": pod,
                "containers": ctr_struct,
            },
        )
    except ApiException as e:
        # 404: không có CR PodMetrics — thường do metrics-server chưa cài, chưa scrape pod,
        # hoặc pod mới (chưa tới kỳ scrape). Không phải lỗi code probe.
        st = getattr(e, "status", None)
        if st == 404:
            msg = (
                f"PodMetrics metrics.k8s.io not found for {ns}/{pod} (HTTP 404). "
                "Typical causes: metrics-server not installed, not yet scraped this pod, "
                "or cluster without Metrics API support."
            )
            logger.info("k8s_clinical_pod_metrics: %s", msg)
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_metrics",
                status="INCONCLUSIVE",
                raw_text=msg,
                structured_hint={
                    "source": "K8s_SDK",
                    "kind": "PodMetrics",
                    "namespace": ns,
                    "pod": pod,
                    "omit_reason": "podmetrics_not_found_404",
                    "http_status": 404,
                },
            )
        logger.warning("k8s_clinical_pod_metrics: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_metrics",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500], "http_status": st},
        )
    except Exception as e:
        logger.warning("k8s_clinical_pod_metrics: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_metrics",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500]},
        )
    finally:
        try:
            await custom.api_client.close()
        except Exception:
            pass


async def probe_k8s_clinical_pod_log_tail(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """Tail 20 dòng log nếu Pod không healthy; nếu healthy thì SKIPPED."""
    ns, pod, _ = pod_identity_from_event(ev)
    if not ns or not pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_tail",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    try:
        p = await v1.read_namespaced_pod(name=pod, namespace=ns)
        if not _pod_needs_log_tail(p):
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_tail",
                status="SKIPPED",
                raw_text="pod looks healthy — log tail skipped (SDK clinical rule)",
                structured_hint={"source": "K8s_SDK", "skipped": True},
            )
        cname = _pick_container_for_log(p)
        if not cname:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_tail",
                status="INCONCLUSIVE",
                raw_text="no container name to read logs",
            )
        log_text = await v1.read_namespaced_pod_log(
            name=pod,
            namespace=ns,
            container=cname,
            tail_lines=20,
            timestamps=True,
        )
        lt = (log_text or "")[:8000]
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_tail",
            status="PASSED",
            raw_text=f"container={cname}\n---\n{lt}",
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodLogTail20",
                "container": cname,
                "lines": 20,
            },
        )
    except Exception as e:
        logger.warning("k8s_clinical_pod_log_tail: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_tail",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500]},
        )
    finally:
        try:
            await v1.api_client.close()
        except Exception:
            pass
