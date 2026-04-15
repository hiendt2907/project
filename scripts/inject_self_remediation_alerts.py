#!/usr/bin/env python3
"""
inject_self_remediation_alerts.py
==================================
Feed the three P0/P1 self-remediation findings into Omni's ingestion pipeline
as Prometheus Alertmanager-style webhook payloads.

Usage:
    # From outside the cluster (port-forward first):
    kubectl port-forward -n multi-agent svc/omni-gateway 8080:80 &
    OMNI_GATEWAY_URL=http://127.0.0.1:8080/webhook/prometheus \\
        python scripts/inject_self_remediation_alerts.py

    # From inside the cluster (default):
    python scripts/inject_self_remediation_alerts.py

    # Inject a specific finding only:
    python scripts/inject_self_remediation_alerts.py --only rbac
    python scripts/inject_self_remediation_alerts.py --only configmap
    python scripts/inject_self_remediation_alerts.py --only oom

Burst / backlog: firing several alerts in a row enqueues multiple ``omni-diagnostic-evidence``
messages; omni-analyst consumes **serially** (commit after full plan path), so Kafka consumer
lag grows between traces. For debugging, inject **one** alert at a time or add sleep between
POSTs; grep logs / Loki using the **trace_id** printed from the gateway JSON response.

Env vars:
    OMNI_GATEWAY_URL     Override gateway URL (default: in-cluster)
    OMNI_INJECT_DRY_RUN  Set to "1" to print payloads without POSTing
    OMNI_INJECT_OOM_POD  Pod name for OmniOomKilledPodNoRecovery labels (default: legacy lab name)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime

import httpx

_GATEWAY_URL = (
    os.environ.get("OMNI_GATEWAY_URL")
    or "http://omni-gateway.multi-agent.svc.cluster.local/webhook/prometheus"
)

_DRY_RUN = os.environ.get("OMNI_INJECT_DRY_RUN", "").strip() == "1"

_NOW = datetime.now(UTC).isoformat(timespec="seconds")


def _alert(alertname: str, namespace: str, labels: dict, annotations: dict) -> dict:
    """Build a minimal Alertmanager-style alert dict."""
    return {
        "status": "firing",
        "labels": {
            "alertname": alertname,
            "namespace": namespace,
            "severity": "critical",
            **labels,
        },
        "annotations": annotations,
        "startsAt": _NOW,
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": f"http://omni-self-remediation/{alertname}",
    }


def _payload(alerts: list[dict], receiver: str = "omni-webhook") -> dict:
    return {
        "version": "4",
        "groupKey": f"{{alertname=\"{alerts[0]['labels']['alertname']}\"}}",
        "status": "firing",
        "receiver": receiver,
        "groupLabels": {"alertname": alerts[0]["labels"]["alertname"]},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# Alert definitions — one per P0/P1 finding
# ---------------------------------------------------------------------------

ALERT_RBAC = _payload([
    _alert(
        alertname="OmniRbacClusterAdminViolation",
        namespace="multi-agent",
        labels={
            "deployment": "omni-executor",
            "service_account": "omni-worker",
            "binding": "omni-worker-cluster-admin",
            "drift_type": "cluster_admin_granted",
            "omni.io/symptom-group": "security_hardening",
            "omni.io/layer": "security",
            "omni_verify_required": "true",
        },
        annotations={
            "summary": "omni-executor SA holds cluster-admin — Zero-Trust P0 violation",
            "description": (
                "ClusterRoleBinding omni-worker-cluster-admin grants cluster-admin to "
                "omni-worker SA shared with omni-executor. "
                "Expected action: k8s_apply_rbac_least_privilege to create scoped Role "
                "in multi-agent and remove the cluster-admin binding."
            ),
            "runbook": "docs/reports/project-memory.md#zero-trust",
        },
    )
])

ALERT_CONFIGMAP = _payload([
    _alert(
        alertname="OmniConfigMapGodModeProd",
        namespace="multi-agent",
        labels={
            "configmap": "omni-worker-config",
            "key": "OMNI_GOD_MODE",
            "current_value": "true",
            "env_mode": "prod",
            "drift_type": "god_mode_in_prod",
            "omni.io/symptom-group": "security_hardening",
            "omni.io/layer": "security",
            "omni_verify_required": "true",
        },
        annotations={
            "summary": "OMNI_GOD_MODE=true in prod ConfigMap — privilege escalation risk",
            "description": (
                "ConfigMap omni-worker-config has OMNI_GOD_MODE=true while "
                "OMNI_ENV_MODE=prod. This violates the prod least-privilege invariant. "
                "Expected action: k8s_patch_configmap to set OMNI_GOD_MODE=false."
            ),
            "runbook": "docs/reports/project-memory.md#lab-prod-isolation",
        },
    )
])

_DEFAULT_OOM_POD = "nginx-load-1775185860"


def _oom_payload(oom_pod: str) -> dict:
    """OOM alert with configurable pod name (labels + annotations)."""
    return _payload([
        _alert(
            alertname="OmniOomKilledPodNoRecovery",
            namespace="multi-agent",
            labels={
                "pod": oom_pod,
                "deployment": "nginx-load",
                "reason": "OOMKilled",
                "duration_days": "6",
                "omni.io/symptom-group": "pod_container_state",
                "omni.io/layer": "workload",
                "omni_verify_required": "true",
            },
            annotations={
                "summary": f"Pod {oom_pod} OOMKilled — no auto-recovery",
                "description": (
                    f"Pod {oom_pod} in multi-agent has been OOMKilled for an extended period "
                    "without recovery. Memory limit may be too low for the workload. "
                    "Expected action: k8s_patch_resource to increase memory limit on the "
                    "nginx-load Deployment."
                ),
                "runbook": "docs/vendor/knownbase.md#oom",
            },
        )
    ])


def _static_alerts() -> dict[str, tuple[str, dict]]:
    return {
        "rbac": ("OmniRbacClusterAdminViolation", ALERT_RBAC),
        "configmap": ("OmniConfigMapGodModeProd", ALERT_CONFIGMAP),
    }


def _post(name: str, payload: dict) -> None:
    # Gateway accepts 8–128 chars [a-zA-Z0-9_-]; use 12 hex chars for X-Omni-Trace-Id.
    client_trace = uuid.uuid4().hex[:12]
    headers = {"Content-Type": "application/json", "X-Omni-Trace-Id": client_trace}
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    if _DRY_RUN:
        print(f"\n[DRY-RUN] {name} (X-Omni-Trace-Id={client_trace})")
        print(body[:600])
        return

    print(
        f"  → POST {_GATEWAY_URL}  alert={name}  X-Omni-Trace-Id={client_trace} ...",
        end="",
        flush=True,
    )
    try:
        r = httpx.post(_GATEWAY_URL, content=body.encode(), headers=headers, timeout=15.0)
        r.raise_for_status()
        pipeline_id = ""
        try:
            js = r.json()
            if isinstance(js, dict):
                pipeline_id = str(js.get("trace_id") or "").strip()
        except Exception:
            pass
        extra = f"  trace_id={pipeline_id}" if pipeline_id else ""
        print(f"  HTTP {r.status_code}{extra}")
    except httpx.HTTPStatusError as e:
        print(f"  HTTP {e.response.status_code} FAILED: {e.response.text[:300]}")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--only",
        choices=list(_static_alerts()) + ["oom"],
        help="Inject only a specific alert (default: inject all three)",
    )
    parser.add_argument(
        "--oom-pod",
        default=None,
        help="Pod name for OOM alert labels (default: OMNI_INJECT_OOM_POD or legacy lab pod)",
    )
    args = parser.parse_args()

    oom_pod = (args.oom_pod or os.environ.get("OMNI_INJECT_OOM_POD", "").strip() or _DEFAULT_OOM_POD).strip()

    all_alerts: dict[str, tuple[str, dict]] = {**_static_alerts(), "oom": ("OmniOomKilledPodNoRecovery", _oom_payload(oom_pod))}

    targets = {args.only: all_alerts[args.only]} if args.only else all_alerts

    mode = "DRY-RUN" if _DRY_RUN else "LIVE"
    print(f"\nOmni self-remediation alert injection [{mode}]")
    print(f"Gateway: {_GATEWAY_URL}")
    if "oom" in targets or not args.only:
        print(f"OOM pod label: {oom_pod}")
    print(f"Injecting {len(targets)} alert(s): {', '.join(targets)}\n")

    for key, (name, payload) in targets.items():
        _post(name, payload)

    print("\nDone. Monitor omni-analyst (plan) and omni-executor (actual mutate) for EXECUTE_MUTATE.")
    print("  kubectl logs -n multi-agent -l app=omni-analyst -f | grep -E 'probe_deterministic_mutate|EXECUTE_MUTATE|rbac|configmap'")
    print("  kubectl logs -n multi-agent -l app=omni-executor -f | grep EXECUTE_MUTATE")
    print(
        "  Executor skips mutations unless OMNI_AUTO_EXECUTE_ENABLED=true "
        "or OMNI_ENV_MODE=dev (see omni-executor deployment / ConfigMap)."
    )


if __name__ == "__main__":
    main()
