#!/usr/bin/env python3
"""Embed k8s/monitor/dashboards/omni/*.json into ConfigMap YAMLs (both mirror paths)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_DIR = ROOT / "k8s/monitor/dashboards/omni"
MAPS: list[tuple[str, list[Path]]] = [
    (
        "omni-l0-command-center.json",
        [
            ROOT / "k8s/monitor/dashboards/configmaps/omni-l0-command-center.yaml",
            ROOT / "k8s/monitor/omni-l0-command-center.yaml",
        ],
    ),
    (
        "omni-l1-system.json",
        [
            ROOT / "k8s/monitor/dashboards/configmaps/omni-l1-system.yaml",
            ROOT / "k8s/monitor/omni-l1-system.yaml",
        ],
    ),
    (
        "omni-l1-cluster.json",
        [
            ROOT / "k8s/monitor/dashboards/configmaps/omni-l1-cluster.yaml",
            ROOT / "k8s/monitor/omni-l1-cluster.yaml",
        ],
    ),
    (
        "omni-pipeline-lgtm.json",
        [
            ROOT / "k8s/monitor/dashboards/configmaps/omni-pipeline-lgtm.yaml",
            ROOT / "k8s/monitor/omni-pipeline-lgtm.yaml",
        ],
    ),
    (
        "omni-l1-proactive.json",
        [
            ROOT / "k8s/monitor/dashboards/configmaps/omni-l1-proactive.yaml",
            ROOT / "k8s/monitor/omni-l1-proactive.yaml",
        ],
    ),
    (
        "omni-l1-learning.json",
        [
            ROOT / "k8s/monitor/dashboards/configmaps/omni-l1-learning.yaml",
            ROOT / "k8s/monitor/omni-l1-learning.yaml",
        ],
    ),
]


def render_configmap(filename: str, json_body: str) -> str:
    key = filename
    header = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboard-{filename.replace(".json", "")}
  namespace: monitor
  labels:
    app: grafana
    grafana_dashboard: "1"
    grafana_folder: omni
data:
  {key}: |
"""
    indented = "\n".join("    " + line if line else "    " for line in json_body.splitlines())
    return header + indented + "\n"


def main() -> None:
    for json_name, yaml_paths in MAPS:
        path = JSON_DIR / json_name
        data = json.loads(path.read_text(encoding="utf-8"))
        body = json.dumps(data, indent=2, ensure_ascii=False)
        out = render_configmap(json_name, body)
        for yp in yaml_paths:
            yp.write_text(out, encoding="utf-8")
            print(f"Wrote {yp.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
