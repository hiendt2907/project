#!/usr/bin/env python3
"""Static gate: prevent known classifier misroute regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from workers.diagnostic_mapping import classify_event, load_diagnostic_matrix  # noqa: E402
from workers.proactive_models import AnomalyEvent  # noqa: E402


def main() -> int:
    matrix = load_diagnostic_matrix(ROOT / "config" / "diagnostic_matrix.yaml")
    ev = AnomalyEvent(
        trace_id="gate-probe-failure",
        error_hint="container waiting createcontainerconfigerror",
        canonical_query=json.dumps(
            {
                "labels": {
                    "alertname": "ProbeFailureLab",
                    "domain": "lab",
                    "reason": "CreateContainerConfigError waiting",
                }
            }
        ),
    )
    row = classify_event(ev, matrix)
    if row is None:
        print("FAIL: classifier returned no match for ProbeFailureLab.", file=sys.stderr)
        return 1
    if row.symptom_group == "ollama_500_context":
        print("FAIL: ProbeFailureLab regressed to ollama_500_context.", file=sys.stderr)
        return 1
    print(f"OK: classifier gate satisfied ({row.symptom_group}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
