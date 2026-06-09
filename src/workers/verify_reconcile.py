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


# ===========================================================================
# Multi-layer claim classification (L1 os_baremetal · L2 network · L3 k8s ·
# remote-agent host metrics). The pod path above is L3 and stays exactly as it
# was; the classifier only ROUTES — it never weakens the pod reconciler.
# ===========================================================================

# Keyword tables per layer. Pod signals (OOM/crash/imagepull/notready/evicted)
# are detected separately by ``detect_claim_signals`` and win the routing tie:
# a "pod OOMKilled" claim must stay on the live-container-status path, not be
# mis-routed to the host OS layer just because it mentions "memory".
_OS_KEYS = (
    "systemd", "service down", "service failed", "unit failed", "unit is down",
    "disk full", "disk is full", "no space", "disk usage", "filesystem full",
    "inode", "df -h", "df -i", "nfs", "mount", "partition",
)
_DB_KEYS = (
    "mysql", "proxysql", "postgres", "postgresql", "mongodb", "mongo",
    "replication", "replica lag", "replication lag", "slave", "primary down",
    "database down", "db down", "connection pool exhausted",
)
_HOSTMETRIC_KEYS = (
    "cpu saturation", "cpu saturated", "high cpu", "cpu spike", "cpu pegged",
    "memory saturation", "mem saturation", "memory pressure", "high memory",
    "memory exhaustion", "host load", "load average", "host cpu", "host mem",
    "host memory", "resource saturation", "saturated",
)
_NETWORK_KEYS = (
    "packet loss", "packet drop", "conntrack", "latency", "rtt",
    "network anomaly", "interface down", "link down", "dns resolution",
    "tcp connection", "time_wait", "syn flood", "retransmit",
)

# Map a host-metric layer to the remote-agent probe + 3σ suffix it reconciles.
_REMOTE_METRICS_PROBE = "remote_system_metrics"
_HOSTMETRIC_FACT_TO_SUFFIX: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("cpu_percent", ("cpu",), "cpu"),
    ("mem_percent", ("memory", "mem"), "mem"),
    ("disk_percent", ("disk", "filesystem"), "disk"),
)
# Mirror remote_host_baseline.REMOTE_KEY_PREFIX without importing the writer
# (read-only here). Kept in sync deliberately — see remote_host_baseline.py:28.
_REMOTE_3SIGMA_PREFIX = "3sigma:remote:"
_REMOTE_3SIGMA_THRESHOLD = 3.0


def _any_kw(text: str, keys: tuple[str, ...]) -> bool:
    return any(k in text for k in keys)


Layer = Literal["pod", "os", "db", "host_metric", "network", ""]


def detect_claim_layer(root_cause: str, affected_workload: str = "") -> Layer:
    """Classify which infrastructure layer the advisory claim lives in.

    Pod-state claims (the original path) take priority so K8s pod failures are
    never down-routed. Otherwise route by keyword to the OS / DB / host-metric /
    network reconciler. Empty string ⇒ no recognized testable layer.
    """
    text = f"{root_cause or ''} {affected_workload or ''}".lower()
    if detect_claim_signals(root_cause):
        return "pod"
    if _any_kw(text, _DB_KEYS):
        return "db"
    if _any_kw(text, _OS_KEYS):
        return "os"
    # host-metric before network: "cpu saturation on host" must not be eaten by
    # a stray "latency" mention; network is the most generic bucket.
    if _any_kw(text, _HOSTMETRIC_KEYS):
        return "host_metric"
    if _any_kw(text, _NETWORK_KEYS):
        return "network"
    return ""


def _evidence_by_probe(ctx: Any) -> dict[str, dict[str, Any]]:
    """Read the probe-evidence map the caller stashed on ctx (read-only).

    Honest gate: when the caller did not attach probe evidence we have nothing
    to test against, so every layered reconciler degrades to ``unverifiable``.
    """
    raw = getattr(ctx, "evidence_by_probe", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _import_os_validator():  # pragma: no cover - thin import shim
    from workers import os_state_validator as osv

    return osv


def reconcile_os_signal(
    probe_name: str, ev: dict[str, Any]
) -> tuple[Verdict, str] | None:
    """Reconcile ONE OS/DB probe envelope against the SYS_HARD_FAIL-style claim.

    Ground truth = ``os_state_validator`` handler output (file:line cited in the
    module docstring of os_state_validator.py). A registered handler returns a
    *contrast string* when the probe PASSED but the alert claims failure → the
    live host contradicts the claim → ``refuted``. A non-PASSED probe with the
    failure indicators present → ``confirmed``. Missing/empty fact ⇒ None
    (caller treats as unverifiable). Never raises.
    """
    try:
        osv = _import_os_validator()
        handler = osv._OS_PROBE_HANDLERS.get(probe_name)
        if handler is None:
            return None
        result = osv._probe_result(ev)
        ef = osv._parse_ef(ev.get("extracted_fact"), probe_name)
        if not ef and result != "FAILED":
            # No extracted fact and not an explicit failure ⇒ unknown state.
            return None
        # PASSED + handler emits contrast ⇒ live host healthy ⇒ claim refuted.
        sanitized = osv._sanitize_probe_ev(ev)
        contrast = handler(sanitized, {})
        if contrast is not None:
            return "refuted", f"{probe_name} PASSED contradicts failure claim — {contrast[:180]}"
        # Handler returned None: either probe FAILED (real fault) or data is
        # insufficient. A FAILED result with the probe present confirms the claim.
        if result and result != "PASSED":
            return "confirmed", f"{probe_name} result={result} confirms failure claim"
        # PASSED but handler withheld contrast ⇒ the probe itself saw a fault
        # (e.g. failed_units present) ⇒ claim stands.
        return "confirmed", f"{probe_name} PASSED but fault indicators present — confirms claim"
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("reconcile_os_signal: probe=%s err=%r", probe_name, exc)
        return None


async def reconcile_os_layer(
    ctx: Any, advisory: Any, *, db: bool = False
) -> ReconcileOutcome:
    """Reconcile OS-state (or DB-health) claims against os_state_validator probes.

    Reuses the existing probe handlers — no reinvention. Read-only, honest gate:
    no relevant probe in the attached evidence ⇒ ``unverifiable``. Can REFUTE: a
    PASSED systemd/disk/mysql probe contradicting a "service down" claim yields
    ``refuted``.
    """
    layer_name = "db" if db else "os"
    relevant = _DB_PROBES if db else _OS_PROBES
    by_probe = _evidence_by_probe(ctx)
    if not by_probe:
        return ReconcileOutcome(
            "unverifiable", f"no probe evidence attached to reconcile {layer_name} claim", ()
        )
    verdicts: list[Verdict] = []
    parts: list[str] = []
    seen: list[str] = []
    for pname, ev in by_probe.items():
        if pname not in relevant or not isinstance(ev, dict):
            continue
        outcome = reconcile_os_signal(pname, ev)
        if outcome is None:
            continue
        v, why = outcome
        verdicts.append(v)
        parts.append(f"{pname}:{v} ({why})")
        seen.append(pname)
    if not verdicts:
        return ReconcileOutcome(
            "unverifiable", f"no testable {layer_name} probe ground-truth present", tuple(seen)
        )
    return ReconcileOutcome(_combine(verdicts), " | ".join(parts), tuple(seen))


_OS_PROBES = frozenset(
    {
        "systemd_units", "disk_usage", "storage_nfs", "raid_mdadm", "lvm_volumes",
        "swap_usage", "service_haproxy", "service_haproxy_prom", "service_nginx",
        "service_keepalived", "cron_jobs", "oom_events", "zombie_processes",
        "kernel_errors", "memory_hw_errors", "docker_daemon", "containerd_state",
    }
)
_DB_PROBES = frozenset(
    {"mysql_health", "proxysql_health", "postgresql_health", "redis_os_health", "mongodb_health"}
)
_NETWORK_PROBES = frozenset(
    {"network_interfaces", "dns_resolution", "tcp_connections"}
)


async def reconcile_network_layer(ctx: Any, advisory: Any) -> ReconcileOutcome:
    """Reconcile network claims against L2 network probes if present.

    Honest gate: when no network probe ran (Prometheus/agent blind), the claim is
    ``unverifiable`` — we never confirm a packet-loss/latency claim we cannot
    observe. Can REFUTE: a PASSED ``network_interfaces``/``tcp_connections`` probe
    contradicting an "interface down"/"connection saturation" claim → ``refuted``.
    """
    by_probe = _evidence_by_probe(ctx)
    if not by_probe:
        return ReconcileOutcome(
            "unverifiable", "no probe evidence attached to reconcile network claim", ()
        )
    verdicts: list[Verdict] = []
    parts: list[str] = []
    seen: list[str] = []
    for pname, ev in by_probe.items():
        if pname not in _NETWORK_PROBES or not isinstance(ev, dict):
            continue
        outcome = reconcile_os_signal(pname, ev)  # same PASSED-contrast logic
        if outcome is None:
            continue
        v, why = outcome
        verdicts.append(v)
        parts.append(f"{pname}:{v} ({why})")
        seen.append(pname)
    if not verdicts:
        return ReconcileOutcome(
            "unverifiable", "no testable network probe ground-truth present", tuple(seen)
        )
    return ReconcileOutcome(_combine(verdicts), " | ".join(parts), tuple(seen))


def _host_from_workload(affected_workload: str) -> str:
    """Remote host id. The agent stamps the hostname in the envelope ``namespace``
    field (see remote_agent/evidence.py:42 + collectors/system.py:73), and the
    advisory mirrors it in affected_workload. Tolerates ``host/...`` or bare host."""
    ns, name = _split_workload(affected_workload)
    return (name or ns).strip()


def _remote_host_from_evidence(by_probe: dict[str, dict[str, Any]]) -> str:
    ev = by_probe.get(_REMOTE_METRICS_PROBE)
    if isinstance(ev, dict):
        return str(ev.get("namespace") or "").strip()
    return ""


async def _remote_zscore(ctx: Any, tenant: str, host: str, suffix: str) -> float | None:
    """Read-only 3σ z-score for a remote host metric from Redis.

    Mirrors ``remote_host_baseline`` keying (3sigma:remote:{tenant}:{host}:{suf})
    via ThreeSigmaGate.get_z_score (read-only — no sample is written). Returns
    None when there is no baseline yet ⇒ caller treats as unverifiable.
    """
    redis = getattr(ctx, "redis", None)
    if redis is None or not host:
        return None
    try:
        from anomaly.three_sigma import ThreeSigmaGate

        gate = ThreeSigmaGate(redis, key_prefix=_REMOTE_3SIGMA_PREFIX)
        metric_id = f"{tenant}:{host}:{suffix}"
        return await gate.get_z_score(metric_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("_remote_zscore: host=%s suffix=%s err=%r", host, suffix, exc)
        return None


async def reconcile_host_metric_layer(ctx: Any, advisory: Any) -> ReconcileOutcome:
    """Reconcile guest-host CPU/mem/disk saturation claims against the remote
    agent payload + Omni-side 3σ baseline (NOT Prometheus — it is blind here).

    Ground truth precedence:
      1. Omni-side 3σ at ``3sigma:remote:{tenant}:{host}:{cpu|mem|disk}`` — if the
         live sample sits within ±3σ of the host's own baseline, a "saturated"
         claim is statistically ``refuted`` (this is the whole point: a number
         that is *high but normal for this host* is not an incident).
      2. Falls back to the agent's PASS/FAIL on the live percentage when no
         baseline exists yet.
    Honest gate: no remote payload AND no baseline ⇒ ``unverifiable``.
    """
    root_cause = str(getattr(advisory, "root_cause", "") or "")
    workload = str(getattr(advisory, "affected_workload", "") or "")
    text = f"{root_cause} {workload}".lower()
    by_probe = _evidence_by_probe(ctx)
    ev = by_probe.get(_REMOTE_METRICS_PROBE) if isinstance(by_probe, dict) else None
    fact: dict[str, Any] = {}
    if isinstance(ev, dict):
        try:
            osv = _import_os_validator()
            fact = osv._parse_ef(ev.get("extracted_fact"), _REMOTE_METRICS_PROBE)
        except Exception:  # noqa: BLE001
            fact = {}

    tenant = str(getattr(getattr(ctx, "settings", None), "tenant_id", "") or "default")
    host = _remote_host_from_evidence(by_probe) or _host_from_workload(workload)

    # Decide which metric the claim is about; default to whatever the text names.
    targets = [
        (fact_key, suffix)
        for (fact_key, kws, suffix) in _HOSTMETRIC_FACT_TO_SUFFIX
        if any(k in text for k in kws)
    ] or [(fk, sx) for (fk, _kw, sx) in _HOSTMETRIC_FACT_TO_SUFFIX]

    verdicts: list[Verdict] = []
    parts: list[str] = []
    seen: list[str] = []
    for fact_key, suffix in targets:
        z = await _remote_zscore(ctx, tenant, host, suffix)
        live = fact.get(fact_key)
        if z is not None:
            seen.append(f"z_{suffix}")
            if abs(z) <= _REMOTE_3SIGMA_THRESHOLD:
                verdicts.append("refuted")
                parts.append(
                    f"{suffix}: z={z:.2f} within ±{_REMOTE_3SIGMA_THRESHOLD}σ of host baseline "
                    f"— '{suffix} saturation' claim refuted (normal for {host or '?'})"
                )
            else:
                verdicts.append("confirmed")
                parts.append(f"{suffix}: z={z:.2f} exceeds ±{_REMOTE_3SIGMA_THRESHOLD}σ — saturation confirmed")
            continue
        # No baseline — fall back to the live agent percentage if present.
        if live is not None:
            seen.append(fact_key)
            try:
                pct = float(live)
            except (TypeError, ValueError):
                continue
            if pct >= 90.0:
                verdicts.append("confirmed")
                parts.append(f"{suffix}: live={pct:.1f}% (no baseline) — saturation confirmed")
            elif pct < 50.0:
                verdicts.append("refuted")
                parts.append(f"{suffix}: live={pct:.1f}% (no baseline) — contradicts saturation claim")
            else:
                parts.append(f"{suffix}: live={pct:.1f}% (no baseline) — ambiguous")

    if not verdicts:
        return ReconcileOutcome(
            "unverifiable",
            "no remote-host baseline or live metric to test saturation claim "
            + (" | ".join(parts) if parts else ""),
            tuple(seen),
        )
    return ReconcileOutcome(_combine(verdicts), " | ".join(parts), tuple(seen))


async def reconcile_advisory(ctx: Any, advisory: Any) -> ReconcileOutcome:
    """Reconcile the advisory's root_cause against live ground truth.

    Routes BY CLAIM LAYER (L1 os_baremetal · L2 network · L3 kubernetes pod ·
    remote-agent host metrics). The pod path is unchanged. Returns an honest
    ground-truth verdict. Best-effort: any failure ⇒ ``unverifiable`` (no
    evidence ⇒ no confirmation). Never raises.
    """
    try:
        root_cause = str(getattr(advisory, "root_cause", "") or "")
        workload = str(getattr(advisory, "affected_workload", "") or "")
        layer = detect_claim_layer(root_cause, workload)

        if layer == "os":
            return await reconcile_os_layer(ctx, advisory, db=False)
        if layer == "db":
            return await reconcile_os_layer(ctx, advisory, db=True)
        if layer == "host_metric":
            return await reconcile_host_metric_layer(ctx, advisory)
        if layer == "network":
            return await reconcile_network_layer(ctx, advisory)

        # Default / "pod" layer — original live-container-status path (unchanged).
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
