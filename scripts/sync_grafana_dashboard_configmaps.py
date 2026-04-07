#!/usr/bin/env python3
"""Sync 3 canonical Grafana dashboards into grafana-dashboards ConfigMap."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_DIR = ROOT / "k8s/monitor/dashboards"
OUT = ROOT / "k8s/monitor/grafana-dashboards.yaml"
FILES = ("omni_ops.json", "omni_security.json", "omni_learning.json")


def _indent_yaml_block(text: str) -> str:
    return "\n".join(("    " + line) if line else "    " for line in text.splitlines())


def main() -> None:
    lines: list[str] = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: grafana-dashboards",
        "  namespace: monitor",
        "  labels:",
        "    app: grafana",
        "    grafana_dashboard: \"1\"",
        "    grafana_folder: omni",
        "data:",
    ]
    for name in FILES:
        path = JSON_DIR / name
        body = json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False)
        lines.append(f"  {name}: |")
        lines.append(_indent_yaml_block(body))

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
