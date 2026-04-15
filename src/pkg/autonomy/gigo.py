"""GIGO: normalize Prometheus / Alertmanager labels into stable K8s-oriented metadata early."""

from __future__ import annotations

def build_gigo_metadata(labels: dict[str, str], annotations: dict[str, str] | None = None) -> dict[str, str]:
    """
    Extract routing fields used downstream (probes, executor namespace gates, dashboards).

    ``error_code`` is a coarse machine string: prefer Kubernetes-ish ``reason`` / drift / phase,
    then ``alertname`` — not a full human summary (that stays in ``error_hint``).
    """
    ann = annotations or {}
    out: dict[str, str] = {}

    def _copy(*keys: str) -> None:
        for k in keys:
            v = labels.get(k)
            if v:
                out[k] = v.strip()

    _copy(
        "namespace",
        "pod",
        "pod_name",
        "deployment",
        "container",
        "container_name",
        "statefulset",
        "daemonset",
        "job",
        "cronjob",
        "service",
        "service_account",
        "node",
        "persistentvolumeclaim",
    )
    if out.get("pod_name") and not out.get("pod"):
        out["pod"] = out["pod_name"]

    an = str(labels.get("alertname") or "").strip()
    if an:
        out["alertname"] = an
    sev = str(labels.get("severity") or "").strip()
    if sev:
        out["severity"] = sev

    for k in ("drift_type", "phase", "reason", "alertstate", "cluster"):
        v = labels.get(k)
        if v:
            out[k] = v.strip()

    err = (
        labels.get("reason")
        or labels.get("drift_type")
        or labels.get("phase")
        or labels.get("alertname")
        or "unknown"
    )
    out["error_code"] = str(err).strip()[:256]

    summ = str(ann.get("summary") or "").strip()
    if summ:
        out["annotation_summary"] = summ[:500]

    return out
