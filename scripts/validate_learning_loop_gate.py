#!/usr/bin/env python3
"""Static gate for baseline self-learning contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_CONSUMER = ROOT / "src" / "workers" / "evidence_consumer.py"
FEEDBACK_LOOP = ROOT / "src" / "workers" / "autonomous_feedback_loop.py"
METRICS = ROOT / "src" / "workers" / "metrics_exporter.py"


def _contains(path: Path, token: str) -> bool:
    try:
        return token in path.read_text(encoding="utf-8")
    except Exception:
        return False


def main() -> int:
    checks = {
        "rag_gate_present": _contains(EVIDENCE_CONSUMER, "evaluate_rag_gate("),
        "planner_present": _contains(EVIDENCE_CONSUMER, "run_agentic_mutate_plan("),
        "proof_gate_present": _contains(EVIDENCE_CONSUMER, "_proof_of_fault_gate("),
        "feedback_writeback_present": _contains(FEEDBACK_LOOP, "_upsert_action_experience_on_success("),
        "learning_metric_upserts": _contains(METRICS, "omni_learning_upserts_total"),
        "learning_metric_experience_saved": _contains(METRICS, "omni_experience_saved_total"),
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        print("FAIL: learning loop gate failed", file=sys.stderr)
        for item in failed:
            print(f" - {item}", file=sys.stderr)
        return 1
    print("OK: learning loop contract gate satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
