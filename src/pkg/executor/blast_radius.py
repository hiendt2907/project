"""Blast-Radius Diff-Scoring + Impact-Tree — code-hard mutate safety gate (plan step 3).

Gemini-sấy lessons baked in:
  1. ``kubectl --dry-run=server`` validates syntax/admission, NOT destructive *logic*:
     ``scale payment-gateway --replicas=0`` returns a green PASS. So blast radius must be
     scored in code, independent of any dry-run verdict.
  2. Counting changed YAML lines is myopic. Impact must follow the K8s **dependency graph**
     (OwnerReferences / label selectors) to count the pods *actually* affected.
  3. K8s **Garbage Collector cascading deletes** must be modelled: deleting a Namespace wipes
     every resource under it; deleting a Deployment/StatefulSet/DaemonSet/ReplicaSet wipes all
     of its child pods via owner-ref cascade. The destructive target is rarely a single object.

A mutate is HARD-BLOCKED (→ HITL) when, regardless of a green dry-run, it would:
  - destroy/restart more than ``max_pods`` pods (impact-tree count), or
  - cut replica capacity by ≥ ``capacity_drop_pct`` (scale-down / scale-to-0), or
  - touch a PVC / StatefulSet storage (data-loss risk), or
  - target an entire Namespace (cluster-section wipe).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Default thresholds (overridable via settings at the call site).
DEFAULT_MAX_PODS = 10
DEFAULT_CAPACITY_DROP_PCT = 20.0

# K8sBlastReader had no explicit timeout — a slow/hung K8s API server could
# head-of-line-block the single-partition mutate-consumer loop indefinitely
# (task #21, HIGH PRIORITY, docs/architecture/OMNI_V2_FINAL_EXECUTION_GATE.md).
K8S_API_TIMEOUT_SEC = 10.0
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SEC = 30.0

# Workload kinds whose deletion cascades to child pods via the GC.
_CASCADING_WORKLOAD_KINDS = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}
)
# Kinds that own storage — deletion/scale-down risks data loss.
_STORAGE_BEARING_KINDS = frozenset({"StatefulSet"})


class ClusterReader(Protocol):
    """Read-only cluster access needed for impact-tree estimation (injectable for tests)."""

    async def list_pod_names(self, namespace: str, label_selector: str | None = None) -> list[str]: ...

    async def workload_selector(self, namespace: str, kind: str, name: str) -> str | None: ...

    async def workload_replicas(self, namespace: str, kind: str, name: str) -> int | None: ...

    async def workload_has_pvc(self, namespace: str, kind: str, name: str) -> bool: ...


@dataclass(frozen=True)
class BlastRadiusVerdict:
    allow: bool
    hard_block: bool
    affected_pods: int
    capacity_drop_pct: float
    touches_storage: bool
    namespace_wide: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def deny_message(self) -> str:
        why = "; ".join(self.reasons) or "blast radius exceeds policy"
        return (
            "[DATA] error\n[DIAGNOSIS] reason_code=ERR_GOV_BLAST_RADIUS "
            f"hard_block affected_pods={self.affected_pods} "
            f"capacity_drop_pct={self.capacity_drop_pct:.0f} "
            f"touches_storage={self.touches_storage} namespace_wide={self.namespace_wide} :: {why}"
        )


# Tools whose blast radius is worth scoring (destructive / wide-impact).
BLAST_SCORED_TOOLS = frozenset(
    {
        "k8s_scale_deployment",
        "k8s_delete_pod",
        "kubectl_cluster",
        "k8s_patch_configmap",
        "k8s_create_or_patch_configmap",
        "k8s_patch_secret",
    }
)


class _K8sApiCircuitBreaker:
    """Process-wide breaker shared by every K8sBlastReader instance.

    K8sBlastReader is constructed fresh per mutate attempt (see
    workers/autonomous_execute.py — a new reader per EXECUTE_MUTATE call), so
    per-instance failure counters would never observe repeated failures
    across separate mutate attempts. This lives at module scope instead so a
    degraded K8s API server trips the breaker after a few consecutive
    timeouts and stops being hammered by every subsequent mutate attempt
    during the cooldown window, instead of each one paying the full timeout.
    """

    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if (time.monotonic() - self._opened_at) >= _CIRCUIT_COOLDOWN_SEC:
            return True  # half-open: let one probe through
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CIRCUIT_FAILURE_THRESHOLD:
            self._opened_at = time.monotonic()


_circuit = _K8sApiCircuitBreaker()


class K8sBlastReader:
    """Live read-only ClusterReader backed by kubernetes-asyncio. Never mutates."""

    def __init__(self) -> None:
        from kubernetes_asyncio import client  # lazy: keep import off unit-test path

        if not _circuit.allow():
            raise RuntimeError(
                "K8s API circuit breaker open (blast-radius reader) — "
                f"cooling down {_CIRCUIT_COOLDOWN_SEC:.0f}s after repeated timeouts"
            )
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()

    @staticmethod
    def _sel(match_labels: dict[str, str] | None) -> str | None:
        if not match_labels:
            return None
        return ",".join(f"{k}={v}" for k, v in sorted(match_labels.items()))

    @staticmethod
    async def _call_with_timeout(coro: Any) -> Any:
        """Bound every real K8s API call; feed the shared circuit breaker."""
        try:
            result = await asyncio.wait_for(coro, timeout=K8S_API_TIMEOUT_SEC)
        except Exception:
            _circuit.record_failure()
            raise
        _circuit.record_success()
        return result

    async def list_pod_names(self, namespace: str, label_selector: str | None = None) -> list[str]:
        plist = await self._call_with_timeout(
            self._core.list_namespaced_pod(namespace, label_selector=label_selector)
        )
        return [p.metadata.name for p in (plist.items or []) if p.metadata and p.metadata.name]

    async def _workload_obj(self, namespace: str, kind: str, name: str) -> Any | None:
        try:
            if kind == "Deployment":
                return await self._call_with_timeout(self._apps.read_namespaced_deployment(name, namespace))
            if kind == "StatefulSet":
                return await self._call_with_timeout(self._apps.read_namespaced_stateful_set(name, namespace))
            if kind == "DaemonSet":
                return await self._call_with_timeout(self._apps.read_namespaced_daemon_set(name, namespace))
            if kind == "ReplicaSet":
                return await self._call_with_timeout(self._apps.read_namespaced_replica_set(name, namespace))
        except Exception:
            return None
        return None

    async def workload_selector(self, namespace: str, kind: str, name: str) -> str | None:
        obj = await self._workload_obj(namespace, kind, name)
        ml = getattr(getattr(getattr(obj, "spec", None), "selector", None), "match_labels", None)
        return self._sel(dict(ml) if ml else None)

    async def workload_replicas(self, namespace: str, kind: str, name: str) -> int | None:
        obj = await self._workload_obj(namespace, kind, name)
        rep = getattr(getattr(obj, "spec", None), "replicas", None)
        return int(rep) if rep is not None else None

    async def workload_has_pvc(self, namespace: str, kind: str, name: str) -> bool:
        if kind == "StatefulSet":
            obj = await self._workload_obj(namespace, kind, name)
            vct = getattr(getattr(obj, "spec", None), "volume_claim_templates", None)
            return bool(vct)
        return False


def _arg(args: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = str((args or {}).get(k) or "").strip()
        if v:
            return v
    return ""


async def _cascade_deleted_pods(
    reader: ClusterReader, namespace: str, kind: str, name: str
) -> tuple[int, bool]:
    """Pods destroyed by a delete of (kind, name), modelling GC cascade. Returns (count, ns_wide).

    - Namespace  → every pod under it is garbage-collected (ns_wide=True).
    - Deployment/StatefulSet/DaemonSet/ReplicaSet/Job → all child pods via label selector.
    - Pod        → itself only (GC does not cascade upward).
    """
    k = (kind or "").strip()
    if k == "Namespace":
        try:
            pods = await reader.list_pod_names(name or namespace, None)
        except Exception:
            pods = []
        return len(pods), True
    if k == "Pod":
        return 1, False
    if k in _CASCADING_WORKLOAD_KINDS:
        try:
            sel = await reader.workload_selector(namespace, k, name)
        except Exception:
            sel = None
        if not sel:
            # Unknown selector — fall back to replica count so we don't under-estimate.
            try:
                rep = await reader.workload_replicas(namespace, k, name)
            except Exception:
                rep = None
            return (int(rep) if rep else 1), False
        try:
            pods = await reader.list_pod_names(namespace, sel)
        except Exception:
            pods = []
        return (len(pods) or 1), False
    return 1, False


async def assess_blast_radius(
    reader: ClusterReader | None,
    *,
    tool: str,
    args: dict[str, Any],
    max_pods: int = DEFAULT_MAX_PODS,
    capacity_drop_pct: float = DEFAULT_CAPACITY_DROP_PCT,
) -> BlastRadiusVerdict:
    """Score a mutate's blast radius via the impact tree. Fail-closed on missing reader.

    Returns ALLOW for low-impact ops (rollout-restart of a small workload, single-pod delete)
    and HARD_BLOCK for destructive logic that a green dry-run would wave through.
    """
    reasons: list[str] = []
    args = dict(args or {})
    namespace = _arg(args, "namespace", "ns")
    tn = (tool or "").strip()

    if reader is None:
        # No live cluster view → cannot bound impact. Fail-closed for destructive verbs.
        if tn in ("k8s_scale_deployment", "kubectl_cluster", "k8s_delete_pod"):
            return BlastRadiusVerdict(
                allow=False, hard_block=True, affected_pods=-1, capacity_drop_pct=0.0,
                touches_storage=False, namespace_wide=False,
                reasons=("no cluster reader — cannot bound blast radius (fail-closed)",),
            )
        return BlastRadiusVerdict(
            allow=True, hard_block=False, affected_pods=0, capacity_drop_pct=0.0,
            touches_storage=False, namespace_wide=False,
            reasons=("reader unavailable; non-destructive tool allowed",),
        )

    affected = 0
    drop_pct = 0.0
    touches_storage = False
    ns_wide = False

    if tn == "k8s_scale_deployment":
        kind = _arg(args, "kind") or "Deployment"
        name = _arg(args, "name", "deployment")
        target = args.get("replicas")
        try:
            target_n = int(target)
        except (TypeError, ValueError):
            target_n = None
        try:
            current = await reader.workload_replicas(namespace, kind, name)
        except Exception:
            current = None
        if current is not None and target_n is not None:
            delta = current - target_n
            affected = max(0, delta)  # pods removed
            if current > 0:
                drop_pct = (max(0, delta) / current) * 100.0
            if target_n == 0:
                reasons.append(f"scale-to-zero on {kind}/{name} (full outage, {current} pods)")
        try:
            touches_storage = await reader.workload_has_pvc(namespace, kind, name)
        except Exception:
            touches_storage = kind in _STORAGE_BEARING_KINDS

    elif tn == "k8s_delete_pod":
        affected = 1

    elif tn == "kubectl_cluster":
        # Break-glass raw verb — inspect for destructive delete.
        verb = _arg(args, "verb", "action").lower()
        raw = " ".join(str(v) for v in args.values()).lower()
        is_delete = verb == "delete" or " delete " in f" {raw} "
        if is_delete:
            kind = _arg(args, "kind", "resource_type", "resource") or (
                "Namespace" if "namespace" in raw or " ns " in raw else "Pod"
            )
            name = _arg(args, "name", "target")
            affected, ns_wide = await _cascade_deleted_pods(reader, namespace, kind, name)
            if ns_wide:
                reasons.append(f"namespace-wide delete cascades to {affected} pods (GC)")
            elif kind in _CASCADING_WORKLOAD_KINDS:
                reasons.append(f"delete {kind}/{name} cascades to {affected} child pods (GC)")
            touches_storage = kind in _STORAGE_BEARING_KINDS

    elif tn in ("k8s_patch_configmap", "k8s_create_or_patch_configmap", "k8s_patch_secret"):
        # Config/secret change triggers a rolling restart of every consuming pod. Without a
        # cheap volume-ref index we approximate with the named workload's pods, else flag wide.
        kind = _arg(args, "kind") or "Deployment"
        name = _arg(args, "name", "deployment", "workload")
        if name:
            cnt, _ = await _cascade_deleted_pods(reader, namespace, kind, name)
            affected = cnt
            reasons.append(f"config change rolling-restarts {cnt} pods of {kind}/{name}")
        else:
            # Unknown consumers → count the whole namespace as the worst case.
            try:
                affected = len(await reader.list_pod_names(namespace, None))
            except Exception:
                affected = 0
            reasons.append("config change with unknown consumers (namespace-wide estimate)")

    # --- Verdict ---
    hard_block = False
    if ns_wide:
        hard_block = True
        reasons.append("entire-namespace blast radius")
    if affected > max_pods:
        hard_block = True
        reasons.append(f"impact-tree {affected} pods > max {max_pods}")
    if drop_pct >= capacity_drop_pct:
        hard_block = True
        reasons.append(f"capacity drop {drop_pct:.0f}% ≥ {capacity_drop_pct:.0f}%")
    if touches_storage:
        hard_block = True
        reasons.append("touches StatefulSet/PVC storage (data-loss risk)")

    return BlastRadiusVerdict(
        allow=not hard_block,
        hard_block=hard_block,
        affected_pods=affected,
        capacity_drop_pct=drop_pct,
        touches_storage=touches_storage,
        namespace_wide=ns_wide,
        reasons=tuple(reasons),
    )
