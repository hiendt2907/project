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


# Log API trả 400 khi container chưa start — lúc đó nên đọc Events.
_SKIP_LOG_WAIT_REASONS = frozenset(
    {"CreateContainerError", "CreateContainerConfigError", "ImagePullBackOff"}
)


def _pending_skip_log_use_events(pod: Any) -> bool:
    """Pending + waiting CreateContainer* / ImagePullBackOff → không tail log; lấy Events."""
    st = getattr(pod, "status", None)
    phase = getattr(st, "phase", None) or ""
    if phase != "Pending":
        return False
    for c in getattr(st, "container_statuses", None) or []:
        if c.state and c.state.waiting:
            rn = (getattr(c.state.waiting, "reason", None) or "").strip()
            if rn in _SKIP_LOG_WAIT_REASONS:
                return True
    return False


async def fetch_pod_events_summary(v1: client.CoreV1Api, ns: str, pod: str, limit: int = 35) -> str:
    try:
        evl = await v1.list_namespaced_event(
            namespace=ns,
            field_selector=f"involvedObject.name={pod},involvedObject.kind=Pod",
        )
    except Exception as e:
        return f"list_namespaced_event failed: {e}"[:2000]
    items = list(getattr(evl, "items", None) or [])

    def _ts(it: Any) -> Any:
        return getattr(it, "last_timestamp", None) or getattr(it, "event_time", None)

    items.sort(key=lambda x: _ts(x) or None, reverse=True)
    lines_out: list[str] = []
    for it in items[:limit]:
        typ = getattr(it, "type", None) or ""
        reason = getattr(it, "reason", None) or ""
        msg = (getattr(it, "message", None) or "")[:400]
        lines_out.append(f"{typ} {reason}: {msg}")
    return "\n".join(lines_out) if lines_out else "(no events in scope)"


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
        waiting_reasons: list[str] = []
        has_crash_loop = False
        has_oom_killed = False
        ready_false = False
        for c in getattr(st, "conditions", None) or []:
            if getattr(c, "type", None) == "Ready" and getattr(c, "status", None) == "False":
                ready_false = True
                break
        for c in getattr(st, "container_statuses", None) or []:
            nm = c.name or "?"
            if c.state and c.state.waiting:
                wr = (getattr(c.state.waiting, "reason", None) or "").strip()
                if wr:
                    waiting_reasons.append(wr)
                    if wr == "CrashLoopBackOff":
                        has_crash_loop = True
                ctr_bits.append(f"{nm}:waiting={c.state.waiting.reason}")
            if c.state and c.state.terminated:
                tr = (getattr(c.state.terminated, "reason", None) or "").strip()
                if tr == "OOMKilled":
                    has_oom_killed = True
                ctr_bits.append(
                    f"{nm}:term={c.state.terminated.reason} exit={c.state.terminated.exit_code}"
                )
        # Dedupe preserve order
        wr_u: list[str] = []
        for w in waiting_reasons:
            if w not in wr_u:
                wr_u.append(w)
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
            "waiting_reasons": wr_u[:20],
            "has_crash_loop": has_crash_loop,
            "has_oom_killed": has_oom_killed,
            "ready_false": ready_false,
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
        if _pending_skip_log_use_events(p):
            ev_txt = await fetch_pod_events_summary(v1, ns, pod)
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_tail",
                status="PASSED",
                raw_text=(
                    "skipped pod log (Pending + CreateContainerError/ImagePullBackOff — "
                    "container not running; log API not useful)\n---\n"
                    + ev_txt
                )[:8000],
                structured_hint={
                    "source": "K8s_SDK",
                    "kind": "PodEvents",
                    "skipped_log": True,
                    "reason": "pending_container_not_started",
                },
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


def _needs_previous_log(pod: Any) -> bool:
    """True when CrashLoopBackOff or terminated container with restart."""
    for c in getattr(pod.status, "container_statuses", None) or []:
        if c.state and c.state.waiting:
            rn = (getattr(c.state.waiting, "reason", None) or "").strip()
            if rn == "CrashLoopBackOff":
                return True
        if c.state and c.state.terminated and (getattr(c, "restart_count", 0) or 0) > 0:
            return True
    return False


async def probe_k8s_clinical_pod_events(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """Pod Events (involvedObject=Pod) — primary signal when container not running."""
    ns, pod, _ = pod_identity_from_event(ev)
    if not ns or not pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_events",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    try:
        ev_txt = await fetch_pod_events_summary(v1, ns, pod)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_events",
            status="PASSED",
            raw_text=ev_txt[:8000],
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodEvents",
                "namespace": ns,
                "pod": pod,
            },
        )
    except Exception as e:
        logger.warning("k8s_clinical_pod_events: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_events",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500]},
        )
    finally:
        try:
            await v1.api_client.close()
        except Exception:
            pass


async def probe_k8s_resource_quota_probe(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """List ResourceQuota in namespace — scheduling / limit issues."""
    ns, pod, _ = pod_identity_from_event(ev)
    if not ns:
        return ProbeRunRaw(
            probe_name="k8s_resource_quota_probe",
            status="SKIPPED",
            raw_text="missing namespace",
        )
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    try:
        rq = await v1.list_namespaced_resource_quota(namespace=ns)
        items = list(getattr(rq, "items", None) or [])
        lines: list[str] = []
        for it in items[:12]:
            name = getattr(getattr(it, "metadata", None), "name", None) or "?"
            st = getattr(it, "status", None)
            hard = getattr(st, "hard", None) if st else None
            used = getattr(st, "used", None) if st else None
            lines.append(f"{name} hard={hard!s} used={used!s}"[:500])
        raw = "\n".join(lines) if lines else "(no ResourceQuota in namespace)"
        return ProbeRunRaw(
            probe_name="k8s_resource_quota_probe",
            status="PASSED",
            raw_text=raw[:4000],
            structured_hint={
                "source": "K8s_SDK",
                "kind": "ResourceQuotaList",
                "namespace": ns,
                "pod": pod,
                "count": len(items),
            },
        )
    except Exception as e:
        logger.warning("k8s_resource_quota_probe: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_resource_quota_probe",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500]},
        )
    finally:
        try:
            await v1.api_client.close()
        except Exception:
            pass


async def probe_k8s_clinical_pod_log_previous(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """Previous container instance log — CrashLoopBackOff / restarts."""
    ns, pod, _ = pod_identity_from_event(ev)
    if not ns or not pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_previous",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    try:
        p = await v1.read_namespaced_pod(name=pod, namespace=ns)
        if not _needs_previous_log(p):
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_previous",
                status="SKIPPED",
                raw_text="no CrashLoopBackOff / restart pattern — previous log not applicable",
                structured_hint={"source": "K8s_SDK", "skipped": True},
            )
        cname = _pick_container_for_log(p)
        if not cname:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_previous",
                status="INCONCLUSIVE",
                raw_text="no container name for previous log",
            )
        log_text = await v1.read_namespaced_pod_log(
            name=pod,
            namespace=ns,
            container=cname,
            tail_lines=40,
            timestamps=True,
            previous=True,
        )
        lt = (log_text or "")[:8000]
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_previous",
            status="PASSED",
            raw_text=f"container={cname} previous=true\n---\n{lt}",
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodLogPrevious",
                "container": cname,
                "lines": 40,
                "k8s_log_previous": True,
            },
        )
    except Exception as e:
        logger.warning("k8s_clinical_pod_log_previous: %s", e)
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_previous",
            status="FAILED",
            raw_text=str(e)[:2000],
            structured_hint={"source": "K8s_SDK", "error": str(e)[:500]},
        )
    finally:
        try:
            await v1.api_client.close()
        except Exception:
            pass
