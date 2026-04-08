#!/usr/bin/env python3
"""Build Alertmanager-style webhook JSON matching Prometheus rule labels (see config/prometheus_alert_label_catalog.yaml)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _resolve_deployment_pod():
    mod_path = Path(__file__).resolve().parent / "workload_resolve_for_tests.py"
    spec = importlib.util.spec_from_file_location("workload_resolve_for_tests", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load workload_resolve_for_tests")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_deployment_pod


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _load_matrix(path: Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    rows = data.get("scenarios") or []
    return [r for r in rows if isinstance(r, dict) and str(r.get("id") or "").strip()]


def _matrix_paths() -> list[Path]:
    raw = os.environ.get("MATRIX_PATHS") or os.environ.get("MATRIX_FILE") or str(
        ROOT / "config" / "incident_training_matrix.yaml"
    )
    parts: list[str] = []
    for sep in (":", ","):
        if sep in raw:
            parts = [p.strip() for p in raw.replace(sep, ",").split(",") if p.strip()]
            break
    if not parts:
        parts = [raw.strip()]
    out: list[Path] = []
    for p in parts:
        out.append(Path(p) if Path(p).is_absolute() else ROOT / p)
    return out


def _scenario_merged(sid: str) -> dict[str, Any]:
    for path in _matrix_paths():
        if not path.exists():
            continue
        rows = _load_matrix(path)
        for row in rows:
            if str(row.get("id") or "").strip() == sid:
                return row
    raise SystemExit(f"scenario not found in matrix: {sid}")


def _load_catalog(path: Path) -> dict[str, Any]:
    data = _load_yaml(path)
    alerts = data.get("alerts") or {}
    return alerts if isinstance(alerts, dict) else {}


def _merge_resolved_series(
    series_keys: list[str],
    defaults: dict[str, str],
    resolved: dict[str, str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in series_keys:
        v = resolved.get(k) or defaults.get(k) or ""
        if v:
            out[k] = v
    return out


def build_payload_for_prometheus_alert(
    alertname: str,
    catalog: dict[str, Any],
    *,
    namespace: str,
    root: Path,
    workload_override: dict[str, Any] | None = None,
    series_defaults_override: dict[str, str] | None = None,
    extra_labels: dict[str, str] | None = None,
    annotations_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolve_deployment_pod = _resolve_deployment_pod()

    entry = catalog.get(alertname)
    if not isinstance(entry, dict):
        raise SystemExit(f"unknown prometheus alert in catalog: {alertname}")

    static_labels = dict(entry.get("static_labels") or {})
    series_keys = list(entry.get("series_label_keys") or [])
    defaults = dict(entry.get("series_label_defaults") or {})
    if series_defaults_override:
        defaults.update({str(k): str(v) for k, v in series_defaults_override.items()})
    ann = dict(entry.get("annotations") or {})

    wr = dict(entry.get("workload_resolve") or {})
    if workload_override:
        wr.update(workload_override)

    resolved_series: dict[str, str] = {}
    if wr.get("deployment"):
        info = resolve_deployment_pod(
            root,
            namespace,
            str(wr["deployment"]),
            container_hint=(str(wr.get("container")).strip() or None),
        )
        resolved_series["namespace"] = info["namespace"]
        resolved_series["pod"] = info["pod"]
        if "container" in series_keys:
            resolved_series["container"] = info.get("container") or ""
        if "uid" in series_keys:
            resolved_series["uid"] = info.get("uid") or ""
        # kubernetes_* style (some scrape rules) — mirror pod if needed for tests
        resolved_series["kubernetes_namespace"] = info["namespace"]
        resolved_series["kubernetes_pod_name"] = info["pod"]

    merged_series = _merge_resolved_series(series_keys, defaults, resolved_series)

    labels: dict[str, str] = {"alertname": alertname}
    labels.update(static_labels)
    labels.update(merged_series)
    if extra_labels:
        for k, v in extra_labels.items():
            if v is not None and str(v).strip():
                labels[str(k)] = str(v)

    if annotations_override:
        ann.update(annotations_override)

    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": labels,
                "annotations": ann,
                "startsAt": "2026-04-07T12:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def build_payload_from_matrix_row(
    sc: dict[str, Any],
    catalog: dict[str, Any],
    *,
    namespace: str,
    root: Path,
) -> dict[str, Any]:
    """Matrix row: prometheus_alert OR alertname matching catalog; optional workload_resolve / annotations_override."""
    pa = str(sc.get("prometheus_alert") or sc.get("alertname") or "").strip()
    # Training-only names → use canonical Prometheus shape
    shape = str(sc.get("prometheus_label_shape") or "").strip()
    if pa not in catalog:
        if shape and shape in catalog:
            pa = shape
        elif "PodCpuUtilizationVsLimitHigh" in catalog:
            pa = "PodCpuUtilizationVsLimitHigh"
        else:
            raise SystemExit(f"scenario {sc.get('id')}: no valid prometheus_alert / catalog entry for {pa!r}")

    wr_override = sc.get("workload_resolve")
    if wr_override is not None and not isinstance(wr_override, dict):
        wr_override = None

    # Optional: only scenario_id on labels for traceability (does not replace PromQL labels).
    extra: dict[str, str] = {}
    sid = str(sc.get("id") or "").strip()
    if sid and sc.get("include_training_labels"):
        for key in ("reason", "domain", "severity", "signal", "workload"):
            if sc.get(key) is not None:
                extra[key] = str(sc.get(key) or "")
    if sid:
        extra["scenario_id"] = sid

    ann_override: dict[str, str] = {}
    if sc.get("summary"):
        ann_override["summary"] = str(sc.get("summary"))
    if sc.get("description"):
        ann_override["description"] = str(sc.get("description"))

    sd_override = sc.get("series_label_defaults")
    if sd_override is not None and not isinstance(sd_override, dict):
        sd_override = None

    return build_payload_for_prometheus_alert(
        pa,
        catalog,
        namespace=namespace,
        root=root,
        workload_override=wr_override,
        series_defaults_override=sd_override,
        extra_labels=extra if extra else None,
        annotations_override=ann_override if ann_override else None,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--matrix",
        default=os.environ.get("MATRIX_FILE") or str(ROOT / "config" / "incident_training_matrix.yaml"),
        help="Primary matrix file; merged with MATRIX_PATHS when resolving scenario-id.",
    )
    ap.add_argument("--catalog", default=str(ROOT / "config" / "prometheus_alert_label_catalog.yaml"))
    ap.add_argument("--scenario-id", required=True)
    ap.add_argument("--namespace", default=os.environ.get("NS", "multi-agent"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    os.environ.setdefault("MATRIX_FILE", args.matrix)
    catalog_path = Path(args.catalog)
    sc = _scenario_merged(args.scenario_id)
    catalog = _load_catalog(catalog_path)

    payload = build_payload_from_matrix_row(
        sc,
        catalog,
        namespace=str(args.namespace),
        root=ROOT,
    )
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
