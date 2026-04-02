#!/usr/bin/env python3
"""Structured simulation log for agentic + dual RAG validation (stdout JSON lines).

Each line: phase, action, trace_id, timestamp, optional fields — grep-friendly for Loki.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from execution.memory_normalize import canonical_symptom_text, extract_workload_fingerprint


def _log(phase: str, action: str, trace_id: str, **extra: object) -> None:
    row: dict[str, object] = {
        "phase": phase,
        "action": action,
        "trace_id": trace_id,
        "timestamp": time.time(),
        **extra,
    }
    print(json.dumps(row, ensure_ascii=False), flush=True)


def main() -> int:
    tid = str(uuid.uuid4())
    _log("prep", "start", tid, kind="simulation")
    sample = (
        "namespace: multi-agent deployment: nginx pod nginx-7d4f8b-xk2zp CrashLoopBackOff "
        "high CPU"
    )
    _log(
        "symptom",
        "canonical_fingerprint",
        tid,
        symptom=canonical_symptom_text(sample),
        workload_fingerprint=extract_workload_fingerprint(sample),
    )
    _log(
        "hint",
        "env",
        tid,
        OMNI_AGENTIC_SLOW_PATH_ENABLED="set true to exercise ReAct path in omni-worker",
        OMNI_OTEL_TRACING_ENABLED="set true with OMNI_OTEL_EXPORTER_OTLP_ENDPOINT for Tempo",
    )
    _log("done", "finish", tid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
