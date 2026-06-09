"""Ground-truth reconciliation for the VERIFY stage.

The advisory LLM self-stamps ``kb_assessment`` verdicts (confirmed/refuted), but
a self-graded verdict is *not* evidence. Trusting it makes VERIFY a rubber stamp:
a hallucinated failure ("pod nginx-test OOMKilled") on a healthy Running pod was
recorded as ``confirmed`` because the LLM said so and *some* probe merely ran.

This module RECONCILES the advisory's ``root_cause`` claim against a live,
read-only read of the claimed pod's container status (phase, restartCount,
lastState.terminated.reason, waiting reasons, Ready condition). The verdict is
derived from that ground truth — never from the LLM's self-report:

  - claim asserts a failure mode that the live pod does NOT exhibit  → ``refuted``
  - claim asserts a failure mode the live pod DOES exhibit           → ``confirmed``
  - not enough evidence to decide (or claim has no testable signal)  → ``unverifiable``

The reconciled verdict then CAPS the LLM's optimism: a ``confirmed`` KB item is
downgraded to ``unverifiable`` (or ``refuted``) when the ground truth does not
support it. We never upgrade a verdict — only honest gating.

Read-only and best-effort: any failure yields an ``unverifiable`` outcome
(honest gate — no evidence ⇒ no confirmation). Never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

Verdict = Literal["confirmed", "refuted", "unverifiable"]

# Failure-mode signals we can test against live container status. Each maps the
# claim keywords (found in root_cause) to the live-state predicate that confirms
# or refutes it.
_OOM_KEYS = ("oomkilled", "oom kill", "out of memory", "working set", "working_set",
             "exceeds the limit", "exceeds limit", "memory limit", "exceeds memory")
_CRASH_KEYS = ("crashloop", "crash loop", "crashing", "restart loop", "restarting", "restarted")
_IMAGEPULL_KEYS = ("imagepull", "image pull", "errimagepull", "imagepullbackoff")
_NOTREADY_KEYS = ("not ready", "unready", "readiness fail", "not-ready", "notready")
_EVICTED_KEYS = ("evicted", "eviction")

_OOM_TERMINATED = frozenset({"OOMKilled"})
_CRASH_WAITING = frozenset({"CrashLoopBackOff"})
_IMAGEPULL_WAITING = frozenset({"ImagePullBackOff", "ErrImagePull", "ImageInspectError"})


@dataclass(frozen=True)
class PodGroundTruth:
    """Live, read-only snapshot of the claimed pod's health."""

    found: bool
    pod_name: str = ""
    phase: str = ""
    restarts: int = 0
    ready: bool = False
    terminated_reasons: frozenset[str] = field(default_factory=frozenset)
    waiting_reasons: frozenset[str] = field(default_factory=frozenset)

    def is_healthy(self) -> bool:
        return (
            self.found
            and self.phase in ("Running", "Succeeded")
            and self.restarts == 0
            and self.ready
            and not self.terminated_reasons
            and not self.waiting_reasons
        )


@dataclass(frozen=True)
class ReconcileOutcome:
    """Result of reconciling the advisory claim against ground truth."""

    verdict: Verdict
    evidence: str
    signals: tuple[str, ...]
    pod: PodGroundTruth | None = None


def detect_claim_signals(root_cause: str) -> tuple[str, ...]:
    """Extract testable failure-mode signals from the advisory root_cause."""
    text = (root_cause or "").lower()
    sigs: list[str] = []
    if any(k in text for k in _OOM_KEYS):
        sigs.append("oom")
    if any(k in text for k in _CRASH_KEYS):
        sigs.append("crash")
    if any(k in text for k in _IMAGEPULL_KEYS):
        sigs.append("imagepull")
    if any(k in text for k in _NOTREADY_KEYS):
        sigs.append("notready")
    if any(k in text for k in _EVICTED_KEYS):
        sigs.append("evicted")
    return tuple(sigs)


def _split_workload(affected_workload: str) -> tuple[str, str]:
    """``namespace/name`` → (namespace, name); tolerates bare name."""
    s = (affected_workload or "").strip()
    if "/" in s:
        ns, _, name = s.partition("/")
        return ns.strip(), name.strip()
    return "", s


async def read_pod_ground_truth(ctx: Any, namespace: str, pod_hint: str) -> PodGroundTruth:
    """Read-only live read of the claimed pod's container status.

    Lists pods in the namespace and matches the hint by exact name or prefix
    (advisories use the deployment/pod base name, e.g. ``nginx-test``, while the
    live pod is ``nginx-test-69c...``). Never raises.
    """
    if not namespace or not pod_hint:
        return PodGroundTruth(found=False)
    try:
        from kubernetes_asyncio import client

        from workers.k8s_tools import _load_k8s_config

        await _load_k8s_config()
        v1 = client.CoreV1Api()
        try:
            resp = await v1.list_namespaced_pod(namespace=namespace)
        finally:
            await v1.api_client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort; missing cluster ⇒ unverifiable
        logger.debug("reconcile: pod read failed ns=%s hint=%s err=%r", namespace, pod_hint, exc)
        return PodGroundTruth(found=False)

    hint = pod_hint.strip().lower()
    match = None
    for p in resp.items or []:
        name = (getattr(p.metadata, "name", "") or "").lower()
        if name == hint or name.startswith(hint) or hint in name:
            match = p
            break
    if match is None:
        return PodGroundTruth(found=False, pod_name=pod_hint)

    st = match.status
    phase = getattr(st, "phase", "") or ""
    restarts = 0
    ready_n = 0
    n_containers = 0
    terminated: set[str] = set()
    waiting: set[str] = set()
    for cs in getattr(st, "container_statuses", None) or []:
        n_containers += 1
        restarts += int(getattr(cs, "restart_count", 0) or 0)
        if getattr(cs, "ready", False):
            ready_n += 1
        state = getattr(cs, "state", None)
        if state and getattr(state, "terminated", None):
            r = getattr(state.terminated, "reason", None)
            if r:
                terminated.add(str(r))
        if state and getattr(state, "waiting", None):
            r = getattr(state.waiting, "reason", None)
            if r:
                waiting.add(str(r))
        last = getattr(cs, "last_state", None)
        if last and getattr(last, "terminated", None):
            r = getattr(last.terminated, "reason", None)
            if r:
                terminated.add(str(r))
    ready = n_containers > 0 and ready_n == n_containers
    return PodGroundTruth(
        found=True,
        pod_name=getattr(match.metadata, "name", "") or pod_hint,
        phase=phase,
        restarts=restarts,
        ready=ready,
        terminated_reasons=frozenset(terminated),
        waiting_reasons=frozenset(waiting),
    )


def reconcile_signal(signal: str, pod: PodGroundTruth) -> tuple[Verdict, str]:
    """Reconcile ONE claim signal against live ground truth."""
    if not pod.found:
        return "refuted", f"claimed pod not found ({pod.pod_name or '?'}) — cannot exhibit '{signal}'"

    desc = f"phase={pod.phase} restarts={pod.restarts} ready={pod.ready}"
    if pod.terminated_reasons:
        desc += f" terminated={sorted(pod.terminated_reasons)}"
    if pod.waiting_reasons:
        desc += f" waiting={sorted(pod.waiting_reasons)}"

    if signal == "oom":
        if pod.terminated_reasons & _OOM_TERMINATED:
            return "confirmed", f"OOMKilled present in container status ({desc})"
        if pod.is_healthy():
            return "refuted", f"pod healthy, no OOMKill — contradicts OOM claim ({desc})"
        return "unverifiable", f"no OOMKill signal but pod not clean-healthy ({desc})"

    if signal == "crash":
        if pod.restarts > 0 or (pod.waiting_reasons & _CRASH_WAITING):
            return "confirmed", f"restarts/crashloop present ({desc})"
        if pod.is_healthy():
            return "refuted", f"0 restarts, Running/Ready — contradicts crash claim ({desc})"
        return "unverifiable", f"no crash signal but pod not clean-healthy ({desc})"

    if signal == "imagepull":
        if pod.waiting_reasons & _IMAGEPULL_WAITING:
            return "confirmed", f"image-pull waiting reason present ({desc})"
        if pod.phase in ("Running", "Succeeded"):
            return "refuted", f"pod is {pod.phase} — image pulled successfully ({desc})"
        return "unverifiable", f"no image-pull signal, phase ambiguous ({desc})"

    if signal == "notready":
        if not pod.ready:
            return "confirmed", f"pod not Ready ({desc})"
        return "refuted", f"pod Ready — contradicts not-ready claim ({desc})"

    if signal == "evicted":
        if pod.phase == "Failed" or ("Evicted" in pod.terminated_reasons):
            return "confirmed", f"eviction present ({desc})"
        if pod.is_healthy():
            return "refuted", f"pod healthy — contradicts eviction claim ({desc})"
        return "unverifiable", f"no eviction signal, pod not clean-healthy ({desc})"

    return "unverifiable", f"untestable signal '{signal}' ({desc})"


def _combine(verdicts: list[Verdict]) -> Verdict:
    """Combine per-signal verdicts: any refute (no confirm) → refuted;
    any confirm → confirmed; else unverifiable."""
    if not verdicts:
        return "unverifiable"
    if "confirmed" in verdicts:
        return "confirmed"
    if "refuted" in verdicts:
        return "refuted"
    return "unverifiable"


async def reconcile_advisory(ctx: Any, advisory: Any) -> ReconcileOutcome:
    """Reconcile the advisory's root_cause against the live state of the claimed pod.

    Returns an honest ground-truth verdict. Best-effort: any failure ⇒
    ``unverifiable`` (no evidence ⇒ no confirmation). Never raises.
    """
    try:
        root_cause = str(getattr(advisory, "root_cause", "") or "")
        workload = str(getattr(advisory, "affected_workload", "") or "")
        signals = detect_claim_signals(root_cause)
        if not signals:
            return ReconcileOutcome("unverifiable", "no testable failure-mode signal in root_cause", ())

        ns, pod_hint = _split_workload(workload)
        if not pod_hint:
            return ReconcileOutcome("unverifiable", "no pod in affected_workload to test", signals)

        pod = await read_pod_ground_truth(ctx, ns, pod_hint)
        per_signal: list[Verdict] = []
        evidence_parts: list[str] = []
        for sig in signals:
            v, why = reconcile_signal(sig, pod)
            per_signal.append(v)
            evidence_parts.append(f"{sig}:{v} ({why})")
        overall = _combine(per_signal)
        return ReconcileOutcome(overall, " | ".join(evidence_parts), signals, pod)
    except Exception as exc:  # noqa: BLE001 — never block dispatch
        logger.warning("event=reconcile_advisory_failed err=%r", exc)
        return ReconcileOutcome("unverifiable", f"reconcile error: {exc}", ())


# Verdict ordering for capping LLM optimism (higher = stronger positive claim).
_RANK: dict[str, int] = {"refuted": 0, "unverifiable": 1, "confirmed": 2}


def cap_assessments(
    assessments: list[dict[str, Any]], ground_truth: Verdict
) -> list[dict[str, Any]]:
    """Cap each KB assessment's verdict by the reconciled ground truth.

    The LLM's verdict is a *hypothesis*; the live probe is the judge.
      - ground_truth ``refuted``      → every applicable item becomes ``refuted``
      - ground_truth ``unverifiable`` → ``confirmed`` items drop to ``unverifiable``
                                        (a refuted item stays refuted)
      - ground_truth ``confirmed``    → keep the LLM verdict (confirmed allowed)
    We never UPGRADE a verdict — only honest gating. Returns new dicts
    (immutable input).
    """
    capped: list[dict[str, Any]] = []
    gt_rank = _RANK.get(ground_truth, 1)
    for a in assessments:
        item = dict(a)
        llm_verdict = str(item.get("verdict", "unverifiable"))
        if ground_truth == "refuted":
            new_verdict: str = "refuted"
        elif ground_truth == "unverifiable":
            # Downgrade confirmed→unverifiable; keep refuted as-is.
            new_verdict = "unverifiable" if _RANK.get(llm_verdict, 1) > gt_rank else llm_verdict
        else:  # confirmed ground truth — LLM verdict stands
            new_verdict = llm_verdict
        if new_verdict != llm_verdict:
            item["reason"] = (
                f"[ground-truth capped {llm_verdict}→{new_verdict}] {item.get('reason', '')}"
            )[:400]
        item["verdict"] = new_verdict
        capped.append(item)
    return capped
