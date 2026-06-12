"""Khám lâm sàng trực tiếp qua K8s API (SDK-first) — real-time, không phụ thuộc Prometheus."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from workers.diagnostic_evidence import ProbeRunRaw
from workers.diagnostic_resource import canonical_flat_labels, pod_identity_from_event
from workers.k8s_tools import _load_k8s_config
from workers.proactive_models import AnomalyEvent
from workers.handlers import WorkerHandlerContext

logger = logging.getLogger(__name__)

_POD_DEPLOYMENT_POD_NAME = re.compile(r"^(.+)-[0-9a-f]{7,10}-[a-z0-9]{5}$")
_MAX_OWNER_DEPTH = 5


@dataclass(frozen=True)
class WorkloadProbeResolution:
    namespace: str
    alert_pod: str
    target_pods: list[str]
    evidence_prefix: str
    meta: dict[str, Any]


def _match_labels_to_selector(ml: dict[str, str] | None) -> str | None:
    if not ml:
        return None
    return ",".join(f"{k}={v}" for k, v in sorted(ml.items()))


def _match_labels_from_workload_obj(obj: Any) -> dict[str, str] | None:
    spec = getattr(obj, "spec", None)
    if not spec:
        return None
    sel = getattr(spec, "selector", None)
    if not sel:
        return None
    ml_raw = getattr(sel, "match_labels", None)
    if ml_raw is None:
        return None
    if isinstance(ml_raw, dict):
        ml = ml_raw
    else:
        ml_d = getattr(ml_raw, "__dict__", None)
        if isinstance(ml_d, dict) and ml_d:
            ml = ml_d
        else:
            return None
    if not ml:
        return None
    return {str(k): str(v) for k, v in ml.items()}


def _infer_workload_name_from_pod_name(pod_name: str) -> str | None:
    m = _POD_DEPLOYMENT_POD_NAME.match(pod_name or "")
    return m.group(1) if m else None


async def _follow_top_controller(
    apps: client.AppsV1Api,
    namespace: str,
    kind: str,
    name: str,
    depth: int,
) -> tuple[str, str] | None:
    if depth > _MAX_OWNER_DEPTH:
        return None
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
        return kind, name
    if kind == "ReplicaSet":
        try:
            rs = await apps.read_namespaced_replica_set(name, namespace)
            for ref in rs.metadata.owner_references or []:
                if ref.kind and ref.name:
                    r = await _follow_top_controller(apps, namespace, ref.kind, ref.name, depth + 1)
                    if r:
                        return r
        except ApiException:
            return None
        return None
    return None


async def _top_controller_from_pod(
    apps: client.AppsV1Api,
    namespace: str,
    pod: Any,
) -> tuple[str, str] | None:
    for ref in pod.metadata.owner_references or []:
        if getattr(ref, "controller", False) and ref.kind and ref.name:
            r = await _follow_top_controller(apps, namespace, ref.kind, ref.name, 0)
            if r:
                return r
    return None


async def resolve_workload_from_pod(namespace: str, pod_name: str) -> tuple[str, str] | None:
    """Resolve a pod to its top controller (kind, name) via OwnerReferences. Read-only.

    Used by the deterministic rollout safety-net when the alert labels lack a
    deployment/workload (e.g. KubePodNotReady carries only namespace+pod). Returns
    ``(kind, name)`` (Deployment/StatefulSet/...) or ``None`` on any failure — never raises.
    """
    if not namespace or not pod_name:
        return None
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        pod_obj = await v1.read_namespaced_pod(pod_name, namespace)
    except Exception:
        return None
    try:
        return await _top_controller_from_pod(apps, namespace, pod_obj)
    except Exception:
        return None


def _pod_ready_false(pod_obj: Any) -> bool:
    """True when the pod's Ready condition is explicitly False, or a container is in a
    fault waiting/terminated state (CrashLoopBackOff/Error). Read-only inspection only."""
    st = getattr(pod_obj, "status", None)
    if st is None:
        return False
    phase = str(getattr(st, "phase", "") or "")
    if phase in ("Failed", "Unknown"):
        return True
    for c in getattr(st, "conditions", None) or []:
        if getattr(c, "type", None) == "Ready" and getattr(c, "status", None) == "False":
            return True
    for cs in getattr(st, "container_statuses", None) or []:
        if getattr(cs, "ready", None) is False:
            waiting = getattr(getattr(cs, "state", None), "waiting", None)
            reason = str(getattr(waiting, "reason", "") or "")
            if reason and reason not in ("ContainerCreating", "PodInitializing"):
                return True
            term = getattr(getattr(cs, "last_state", None), "terminated", None)
            if term is not None and str(getattr(term, "reason", "") or ""):
                return True
    return False


async def confirm_workload_unavailable(namespace: str, pod_name: str) -> bool:
    """Live read-only ground-truth check: is the alert's target genuinely NOT ready?

    Physical-proof source for the deterministic rollout safety-net (KubePodNotReady /
    workload-unavailable faults carry no sigma/critical evidence). Reads the named pod
    first; if it is gone, resolves the owning workload and checks its current pods. Returns
    True only when a real Ready=False / fault state is observed — never trusts the alert
    claim. Returns False on any error (fail-closed: no proof ⇒ gate keeps blocking).
    """
    if not namespace or not pod_name:
        return False
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
    except Exception:
        return False
    try:
        pod_obj = await v1.read_namespaced_pod(pod_name, namespace)
        return _pod_ready_false(pod_obj)
    except ApiException as exc:
        if getattr(exc, "status", None) != 404:
            return False
    except Exception:
        return False
    # Named pod is gone — confirm via the owning workload's live pods (rotated replica).
    try:
        ctrl = await resolve_workload_from_pod(namespace, pod_name)
    except Exception:
        ctrl = None
    if ctrl is None:
        inferred = _infer_workload_name_from_pod_name(pod_name)
        if not inferred:
            return False
        pods = await _try_list_by_deployment_name(v1, apps, namespace, inferred) or []
    else:
        try:
            batch_api = client.BatchV1Api()
            pods = await _list_pod_names_for_workload(
                v1, apps, batch_api, namespace, ctrl[0], ctrl[1]
            )
        except Exception:
            pods = []
    for pname in pods:
        try:
            p = await v1.read_namespaced_pod(pname, namespace)
        except Exception:
            continue
        if _pod_ready_false(p):
            return True
    return False


async def _list_pod_names_for_workload(
    v1: client.CoreV1Api,
    apps: client.AppsV1Api,
    batch_api: client.BatchV1Api,
    namespace: str,
    kind: str,
    name: str,
) -> list[str]:
    ml: dict[str, str] | None = None
    # Defensive: callers may pass uninitialized clients (e.g. when every prior
    # resolution strategy already failed). Treat a missing client as "no pods" rather
    # than raising AttributeError mid-resolution.
    if apps is None or (kind == "Job" and batch_api is None):
        return []
    try:
        if kind == "Deployment":
            obj = await apps.read_namespaced_deployment(name, namespace)
            ml = _match_labels_from_workload_obj(obj)
        elif kind == "StatefulSet":
            obj = await apps.read_namespaced_stateful_set(name, namespace)
            ml = _match_labels_from_workload_obj(obj)
        elif kind == "DaemonSet":
            obj = await apps.read_namespaced_daemon_set(name, namespace)
            ml = _match_labels_from_workload_obj(obj)
        elif kind == "Job":
            obj = await batch_api.read_namespaced_job(name, namespace)
            ml = _match_labels_from_workload_obj(obj)
        elif kind == "CronJob":
            return []
    except ApiException:
        return []
    sel = _match_labels_to_selector(ml)
    if not sel:
        return []
    try:
        plist = await v1.list_namespaced_pod(namespace, label_selector=sel)
    except ApiException:
        return []
    out = [p.metadata.name for p in (plist.items or []) if p.metadata and p.metadata.name]
    return sorted(set(out))


async def _try_list_by_deployment_name(
    v1: client.CoreV1Api,
    apps: client.AppsV1Api,
    namespace: str,
    dep_name: str,
) -> list[str] | None:
    try:
        dep = await apps.read_namespaced_deployment(dep_name, namespace)
        ml = _match_labels_from_workload_obj(dep)
        sel = _match_labels_to_selector(ml)
        if not sel:
            return None
        plist = await v1.list_namespaced_pod(namespace, label_selector=sel)
        return sorted(
            {p.metadata.name for p in (plist.items or []) if p.metadata and p.metadata.name}
        )
    except ApiException:
        return None


async def _try_list_by_label_eq(
    v1: client.CoreV1Api,
    namespace: str,
    key: str,
    value: str,
) -> list[str] | None:
    if not key or not value:
        return None
    try:
        plist = await v1.list_namespaced_pod(namespace, label_selector=f"{key}={value}")
        return sorted(
            {p.metadata.name for p in (plist.items or []) if p.metadata and p.metadata.name}
        )
    except ApiException:
        return None


async def _resolve_pods_when_pod_missing(
    v1: client.CoreV1Api,
    apps: client.AppsV1Api,
    batch_api: client.BatchV1Api,
    namespace: str,
    alert_pod: str,
    labels: dict[str, str],
) -> tuple[list[str], str]:
    dep_label = (labels.get("deployment") or labels.get("deployment_name") or "").strip()
    if dep_label:
        got = await _try_list_by_deployment_name(v1, apps, namespace, dep_label)
        if got:
            return got, "label_deployment"

    for lk in ("app", "k8s-app", "app.kubernetes.io/name"):
        av = (labels.get(lk) or "").strip()
        if av:
            got = await _try_list_by_label_eq(v1, namespace, lk, av)
            if got:
                return got, f"label_{lk}"

    jn = (labels.get("job-name") or labels.get("job_name") or "").strip()
    if jn:
        try:
            job = await batch_api.read_namespaced_job(jn, namespace)
            ml = _match_labels_from_workload_obj(job)
            sel = _match_labels_to_selector(ml)
            if sel:
                plist = await v1.list_namespaced_pod(namespace, label_selector=sel)
                got = sorted(
                    {p.metadata.name for p in (plist.items or []) if p.metadata and p.metadata.name}
                )
                if got:
                    return got, "label_job-name"
        except ApiException:
            pass

    inferred = _infer_workload_name_from_pod_name(alert_pod)
    if inferred:
        got = await _try_list_by_deployment_name(v1, apps, namespace, inferred)
        if got:
            return got, "pod_name_pattern_deployment"

    # Final fallback: the alert "pod" label may actually carry the bare workload
    # name (no replica hash) — common when the deployment/app label is stripped
    # during canonicalization. Treat alert_pod itself as a candidate workload so a
    # rotated/abstracted pod reference still resolves to live pods instead of
    # collapsing to "unresolved" (which strands the whole state lane). F14.
    if alert_pod:
        for kind in ("Deployment", "StatefulSet"):
            got = await _list_pod_names_for_workload(
                v1, apps, batch_api, namespace, kind, alert_pod
            )
            if got:
                return got, f"alert_pod_as_{kind.lower()}"

    return [], "unresolved"


async def resolve_workload_probe_targets(
    v1: client.CoreV1Api,
    apps: client.AppsV1Api,
    batch_api: client.BatchV1Api,
    ev: AnomalyEvent,
    namespace: str,
    alert_pod: str,
) -> WorkloadProbeResolution:
    """
    Resolve alert pod → parent workload (OwnerReferences) or labels/heuristics when the pod is gone.
    Never rely on a single stale pod name for evidence collection.
    """
    labels = canonical_flat_labels(ev)
    meta: dict[str, Any] = {
        "namespace": namespace,
        "alert_pod": alert_pod,
        "labels_used": sorted(labels.keys()),
    }
    pod_obj: Any | None = None
    try:
        pod_obj = await v1.read_namespaced_pod(alert_pod, namespace)
    except ApiException as e:
        if getattr(e, "status", None) != 404:
            raise
    if pod_obj is not None:
        ctl = await _top_controller_from_pod(apps, namespace, pod_obj)
        if ctl:
            kind, wname = ctl
            pods = await _list_pod_names_for_workload(v1, apps, batch_api, namespace, kind, wname)
            meta.update({"workload_kind": kind, "workload_name": wname, "resolution": "owner_reference"})
            if not pods:
                pods = [alert_pod]
            prefix = (
                f"Alert pod tracked to parent workload {kind}/{wname}. "
                f"Investigating current active pods: {', '.join(pods)}."
            )
            return WorkloadProbeResolution(namespace, alert_pod, pods, prefix, meta)
        meta["resolution"] = "pod_only_no_controller"
        prefix = f"Alert pod has no controller in OwnerReferences; investigating pod: {alert_pod}."
        return WorkloadProbeResolution(namespace, alert_pod, [alert_pod], prefix, meta)

    pods, how = await _resolve_pods_when_pod_missing(v1, apps, batch_api, namespace, alert_pod, labels)
    meta["resolution"] = f"missing_pod:{how}"
    if pods:
        prefix = (
            f"Alert pod {alert_pod} not found (likely rotated). "
            f"Investigating current active pods: {', '.join(pods)}."
        )
    else:
        prefix = (
            f"Alert pod {alert_pod} not found (likely rotated); could not list replacement pods "
            f"from labels ({how})."
        )
    return WorkloadProbeResolution(namespace, alert_pod, pods, prefix, meta)


def _memory_limits_from_pod_spec(pod: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """When PodMetrics CR is missing (404), recover declared limits from Pod spec."""
    lines: list[str] = []
    ctr_struct: list[dict[str, Any]] = []
    for c in getattr(pod.spec, "containers", None) or []:
        nm = getattr(c, "name", None) or "?"
        mem: str | None = None
        res = getattr(c, "resources", None)
        if res:
            lim = getattr(res, "limits", None)
            if lim:
                if isinstance(lim, dict):
                    mem = str(lim.get("memory") or lim.get("Memory") or "") or None
                else:
                    m = getattr(lim, "memory", None)
                    mem = str(m) if m else None
        if mem:
            lines.append(f"{nm}: memory_limit_spec={mem}")
            ctr_struct.append(
                {
                    "name": nm,
                    "cpu": None,
                    "memory": mem,
                    "source": "spec_limits_fallback",
                }
            )
    return lines, ctr_struct


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


def _single_pod_status_fragment(ns: str, pod_name: str, p: Any) -> tuple[str, dict[str, Any]]:
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
    max_restart_count = 0
    for c in getattr(st, "container_statuses", None) or []:
        nm = c.name or "?"
        rc = int(getattr(c, "restart_count", 0) or 0)
        if rc > max_restart_count:
            max_restart_count = rc
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
            ctr_bits.append(f"{nm}:term={c.state.terminated.reason} exit={c.state.terminated.exit_code}")
        # A crash-looping pod oscillates Running<->Terminated; when sampled mid-Running
        # the *current* state looks healthy. The canonical K8s signals for "recently
        # crashed" are last_state.terminated + restart_count — consult them so a real
        # OOM/crash fault is not dismissed as a false alarm (proof-of-fault gate).
        ls = getattr(c, "last_state", None)
        ls_term = getattr(ls, "terminated", None) if ls else None
        if ls_term is not None:
            ls_reason = (getattr(ls_term, "reason", None) or "").strip()
            ls_exit = getattr(ls_term, "exit_code", None)
            if ls_reason == "OOMKilled":
                has_oom_killed = True
            # Repeated non-zero-exit terminations = effective crash loop even if the
            # current snapshot caught the container momentarily Running.
            if rc >= 2 and (ls_reason in ("OOMKilled", "Error") or (ls_exit not in (None, 0))):
                has_crash_loop = True
            if ls_reason or ls_exit not in (None, 0):
                ctr_bits.append(
                    f"{nm}:last_term={ls_reason or '?'} exit={ls_exit} restarts={rc}"
                )
    wr_u: list[str] = []
    for w in waiting_reasons:
        if w not in wr_u:
            wr_u.append(w)
    body = (
        f"phase={phase}\nconditions={'; '.join(cond_bits) or 'none'}\n"
        f"containers={'; '.join(ctr_bits) or 'none'}"
    )[:4000]
    frag = f"=== pod/{pod_name} ===\n{body}"
    structured: dict[str, Any] = {
        "pod": pod_name,
        "namespace": ns,
        "phase": phase,
        "conditions": cond_bits[:20],
        "container_signals": ctr_bits[:20],
        "waiting_reasons": wr_u[:20],
        "has_crash_loop": has_crash_loop,
        "has_oom_killed": has_oom_killed,
        "ready_false": ready_false,
        "restart_count": max_restart_count,
    }
    return frag, structured


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
    v1: client.CoreV1Api | None = None
    apps: client.AppsV1Api | None = None
    batch_api: client.BatchV1Api | None = None
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        res = await resolve_workload_probe_targets(v1, apps, batch_api, ev, ns, pod)
        prefix = res.evidence_prefix
        if not res.target_pods:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_status",
                status="INCONCLUSIVE",
                raw_text=prefix + "\n(no active pods to inspect).",
                structured_hint={"source": "K8s_SDK", "kind": "PodStatus", **res.meta},
            )
        frags: list[str] = []
        per_pod: list[dict[str, Any]] = []
        any_crash = any_oom = any_ready_false = False
        for pname in res.target_pods:
            try:
                p = await v1.read_namespaced_pod(name=pname, namespace=ns)
            except ApiException as ex:
                frags.append(f"=== pod/{pname} ===\n(read failed: {getattr(ex, 'status', '?')} {getattr(ex, 'reason', ex)})")
                continue
            except Exception as ex:
                frags.append(f"=== pod/{pname} ===\n(read failed: {ex})")
                continue
            frag, st = _single_pod_status_fragment(ns, pname, p)
            frags.append(frag)
            per_pod.append(st)
            if st.get("has_crash_loop"):
                any_crash = True
            if st.get("has_oom_killed"):
                any_oom = True
            if st.get("ready_false"):
                any_ready_false = True
        raw = (prefix + "\n\n" + "\n\n".join(frags))[:12000]
        structured: dict[str, Any] = {
            "source": "K8s_SDK",
            "kind": "PodStatus",
            "namespace": ns,
            "alert_pod": pod,
            "target_pods": res.target_pods,
            "pods": per_pod,
            "has_crash_loop": any_crash,
            "has_oom_killed": any_oom,
            "ready_false": any_ready_false,
            **res.meta,
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
        for api in (x for x in (v1, apps, batch_api) if x is not None):
            try:
                await api.api_client.close()
            except Exception:
                pass


async def probe_k8s_clinical_pod_metrics(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """metrics.k8s.io/v1beta1 PodMetrics — CPU/Mem usage tại thời điểm gọi."""
    ns, alert_pod, _ = pod_identity_from_event(ev)
    if not ns or not alert_pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_metrics",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1: client.CoreV1Api | None = None
    apps: client.AppsV1Api | None = None
    batch_api: client.BatchV1Api | None = None
    custom: client.CustomObjectsApi | None = None
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        custom = client.CustomObjectsApi()
        res = await resolve_workload_probe_targets(v1, apps, batch_api, ev, ns, alert_pod)
        prefix = res.evidence_prefix
        targets = res.target_pods if res.target_pods else [alert_pod]
        blocks: list[str] = []
        ctr_all: list[dict[str, Any]] = []
        saw_metrics = False
        last_non404: ApiException | None = None
        for pod_name in targets:
            try:
                obj = await custom.get_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="pods",
                    name=pod_name,
                )
            except ApiException as e:
                st = getattr(e, "status", None)
                if st != 404:
                    last_non404 = e
                continue
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
                ctr_struct.append({"pod": pod_name, "name": nm, "cpu": cpu, "memory": mem})
            blk = f"=== pod/{pod_name} ===\n" + ("\n".join(lines) if lines else "no containers in PodMetrics")
            blocks.append(blk[:2000])
            ctr_all.extend(ctr_struct)
            if lines:
                saw_metrics = True
        if saw_metrics:
            raw = (prefix + "\n\n" + "\n\n".join(blocks))[:4000]
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_metrics",
                status="PASSED",
                raw_text=raw,
                structured_hint={
                    "source": "K8s_SDK",
                    "kind": "PodMetrics",
                    "namespace": ns,
                    "alert_pod": alert_pod,
                    "target_pods": targets,
                    "containers": ctr_all,
                    **res.meta,
                },
            )
        msg = (
            f"PodMetrics metrics.k8s.io not found for workload pods in {ns} (HTTP 404). "
            "Typical causes: metrics-server not installed, not yet scraped these pods, "
            "or cluster without Metrics API support."
        )
        logger.info("k8s_clinical_pod_metrics: %s", msg)
        for pod_name in targets:
            try:
                pobj = await v1.read_namespaced_pod(name=pod_name, namespace=ns)
            except Exception as ex:
                logger.info(
                    "k8s_clinical_pod_metrics: spec_limits_fallback_failed ns=%s pod=%s err=%s",
                    ns,
                    pod_name,
                    ex,
                )
                continue
            spec_lines, ctr_struct = _memory_limits_from_pod_spec(pobj)
            if spec_lines:
                raw_fb = (
                    prefix
                    + "\n\n"
                    + msg
                    + f"\nFallback (Pod.spec.containers[].resources.limits.memory) for pod/{pod_name}:\n"
                    + "\n".join(spec_lines)
                )[:4000]
                return ProbeRunRaw(
                    probe_name="k8s_clinical_pod_metrics",
                    status="PASSED",
                    raw_text=raw_fb,
                    structured_hint={
                        "source": "K8s_SDK",
                        "kind": "PodMetricsSpecFallback",
                        "namespace": ns,
                        "alert_pod": alert_pod,
                        "target_pods": targets,
                        "pod": pod_name,
                        "omit_reason": "podmetrics_not_found_404_spec_limits_used",
                        "http_status": 404,
                        "containers": ctr_struct,
                        **res.meta,
                    },
                )
        if last_non404 is not None:
            e = last_non404
            st = getattr(e, "status", None)
            logger.warning("k8s_clinical_pod_metrics: %s", e)
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_metrics",
                status="FAILED",
                raw_text=str(e)[:2000],
                structured_hint={"source": "K8s_SDK", "error": str(e)[:500], "http_status": st},
            )
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_metrics",
            status="INCONCLUSIVE",
            raw_text=prefix + "\n\n" + msg,
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodMetrics",
                "namespace": ns,
                "alert_pod": alert_pod,
                "target_pods": targets,
                "omit_reason": "podmetrics_not_found_404",
                "http_status": 404,
                **res.meta,
            },
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
        for api in (x for x in (v1, apps, batch_api, custom) if x is not None):
            try:
                await api.api_client.close()
            except Exception:
                pass


async def probe_k8s_clinical_pod_log_tail(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """Tail 20 dòng log nếu Pod không healthy; nếu healthy thì SKIPPED."""
    ns, alert_pod, _ = pod_identity_from_event(ev)
    if not ns or not alert_pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_tail",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1: client.CoreV1Api | None = None
    apps: client.AppsV1Api | None = None
    batch_api: client.BatchV1Api | None = None
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        res = await resolve_workload_probe_targets(v1, apps, batch_api, ev, ns, alert_pod)
        prefix = res.evidence_prefix
        if not res.target_pods:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_tail",
                status="INCONCLUSIVE",
                raw_text=prefix + "\n(no active pods to inspect).",
                structured_hint={"source": "K8s_SDK", **res.meta},
            )
        needs_any = False
        for pname in res.target_pods:
            try:
                p = await v1.read_namespaced_pod(name=pname, namespace=ns)
            except Exception:
                continue
            if _pod_needs_log_tail(p):
                needs_any = True
                break
        if not needs_any:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_tail",
                status="SKIPPED",
                raw_text=prefix + "\nAll resolved workload pods look healthy — log tail skipped (SDK clinical rule).",
                structured_hint={"source": "K8s_SDK", "skipped": True, **res.meta},
            )
        parts: list[str] = []
        for pname in res.target_pods:
            try:
                p = await v1.read_namespaced_pod(name=pname, namespace=ns)
            except Exception as ex:
                parts.append(f"=== pod/{pname} ===\n(read failed: {ex})")
                continue
            if not _pod_needs_log_tail(p):
                continue
            if _pending_skip_log_use_events(p):
                ev_txt = await fetch_pod_events_summary(v1, ns, pname)
                parts.append(
                    f"=== pod/{pname} ===\n"
                    "skipped pod log (Pending + CreateContainerError/ImagePullBackOff — "
                    "container not running; log API not useful)\n---\n"
                    + ev_txt
                )
                continue
            cname = _pick_container_for_log(p)
            if not cname:
                parts.append(f"=== pod/{pname} ===\n(no container name to read logs)")
                continue
            log_text = await v1.read_namespaced_pod_log(
                name=pname,
                namespace=ns,
                container=cname,
                tail_lines=20,
                timestamps=True,
            )
            lt = (log_text or "")[:4000]
            parts.append(f"=== pod/{pname} ===\ncontainer={cname}\n---\n{lt}")
        if not parts:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_tail",
                status="INCONCLUSIVE",
                raw_text=prefix + "\n(no log/event fragments collected).",
                structured_hint={"source": "K8s_SDK", **res.meta},
            )
        raw = (prefix + "\n\n" + "\n\n".join(parts))[:8000]
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_tail",
            status="PASSED",
            raw_text=raw,
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodLogTail20",
                "lines": 20,
                "alert_pod": alert_pod,
                "target_pods": res.target_pods,
                **res.meta,
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
        for api in (x for x in (v1, apps, batch_api) if x is not None):
            try:
                await api.api_client.close()
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
    ns, alert_pod, _ = pod_identity_from_event(ev)
    if not ns or not alert_pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_events",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1: client.CoreV1Api | None = None
    apps: client.AppsV1Api | None = None
    batch_api: client.BatchV1Api | None = None
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        res = await resolve_workload_probe_targets(v1, apps, batch_api, ev, ns, alert_pod)
        prefix = res.evidence_prefix
        targets = res.target_pods if res.target_pods else [alert_pod]
        parts: list[str] = []
        for pname in targets:
            ev_txt = await fetch_pod_events_summary(v1, ns, pname)
            parts.append(f"=== pod/{pname} ===\n{ev_txt}")
        raw = (prefix + "\n\n" + "\n\n".join(parts))[:8000]
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_events",
            status="PASSED",
            raw_text=raw,
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodEvents",
                "namespace": ns,
                "alert_pod": alert_pod,
                "target_pods": targets,
                **res.meta,
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
        for api in (x for x in (v1, apps, batch_api) if x is not None):
            try:
                await api.api_client.close()
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
    v1: client.CoreV1Api | None = None
    try:
        v1 = client.CoreV1Api()
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
        if v1 is not None:
            try:
                await v1.api_client.close()
            except Exception:
                pass


async def probe_k8s_clinical_pod_log_previous(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """Previous container instance log — CrashLoopBackOff / restarts."""
    ns, alert_pod, _ = pod_identity_from_event(ev)
    if not ns or not alert_pod:
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_previous",
            status="SKIPPED",
            raw_text="missing namespace or pod",
        )
    await _load_k8s_config()
    v1: client.CoreV1Api | None = None
    apps: client.AppsV1Api | None = None
    batch_api: client.BatchV1Api | None = None
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        res = await resolve_workload_probe_targets(v1, apps, batch_api, ev, ns, alert_pod)
        prefix = res.evidence_prefix
        if not res.target_pods:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_previous",
                status="INCONCLUSIVE",
                raw_text=prefix + "\n(no active pods to inspect).",
                structured_hint={"source": "K8s_SDK", **res.meta},
            )
        needs_any = False
        for pname in res.target_pods:
            try:
                p = await v1.read_namespaced_pod(name=pname, namespace=ns)
            except Exception:
                continue
            if _needs_previous_log(p):
                needs_any = True
                break
        if not needs_any:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_previous",
                status="SKIPPED",
                raw_text=prefix + "\nno CrashLoopBackOff / restart pattern on resolved pods — previous log not applicable",
                structured_hint={"source": "K8s_SDK", "skipped": True, **res.meta},
            )
        parts: list[str] = []
        for pname in res.target_pods:
            try:
                p = await v1.read_namespaced_pod(name=pname, namespace=ns)
            except Exception as ex:
                parts.append(f"=== pod/{pname} ===\n(read failed: {ex})")
                continue
            if not _needs_previous_log(p):
                continue
            cname = _pick_container_for_log(p)
            if not cname:
                parts.append(f"=== pod/{pname} ===\n(no container name for previous log)")
                continue
            log_text = await v1.read_namespaced_pod_log(
                name=pname,
                namespace=ns,
                container=cname,
                tail_lines=40,
                timestamps=True,
                previous=True,
            )
            lt = (log_text or "")[:4000]
            parts.append(f"=== pod/{pname} ===\ncontainer={cname} previous=true\n---\n{lt}")
        if not parts:
            return ProbeRunRaw(
                probe_name="k8s_clinical_pod_log_previous",
                status="INCONCLUSIVE",
                raw_text=prefix + "\n(no previous log fragments collected).",
                structured_hint={"source": "K8s_SDK", **res.meta},
            )
        raw = (prefix + "\n\n" + "\n\n".join(parts))[:8000]
        return ProbeRunRaw(
            probe_name="k8s_clinical_pod_log_previous",
            status="PASSED",
            raw_text=raw,
            structured_hint={
                "source": "K8s_SDK",
                "kind": "PodLogPrevious",
                "lines": 40,
                "k8s_log_previous": True,
                "alert_pod": alert_pod,
                "target_pods": res.target_pods,
                **res.meta,
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
        for api in (x for x in (v1, apps, batch_api) if x is not None):
            try:
                await api.api_client.close()
            except Exception:
                pass
