#!/usr/bin/env python3
"""
Run pytest with coverage on src/, then print waves + top gaps.

Usage (from repo root):
  .venv/bin/python scripts/coverage_gap_report.py
  .venv/bin/python scripts/coverage_gap_report.py --top 50

Waves (heuristic — adjust WAVE_RULES as the tree evolves):
  W1  Control plane & contracts: gateway, pkg, services, messaging, llm (no workers)
  W2  Worker orchestration: workers/* except W3
  W3  Heavy IO / clinical: k8s_*, diagnostic_*, proactive_*, baseline_*, rag/redis_vector_store
  WX  Tooling / samples (optional exclude from ratchet): training, knowledge, devtools, visualization, opensandbox_shim
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_ROW_RE = re.compile(
    r"^(src/\S+)\s+(\d+)\s+(\d+)\s+([\d.]+)%"
)


@dataclass
class FileCov:
    path: str
    statements: int
    missing: int
    pct: float

    @property
    def covered(self) -> int:
        return self.statements - self.missing


def _wave_for(path: str) -> str:
    if path.startswith("src/training/") or path.startswith("src/knowledge/"):
        return "WX"
    if path.startswith("src/devtools/") or path.startswith("src/visualization/"):
        return "WX"
    if path.startswith("src/opensandbox_shim/") or path.startswith("src/sre/"):
        return "WX"
    if path.startswith("src/workers/"):
        base = path.rsplit("/", 1)[-1]
        if base.startswith(("k8s_", "diagnostic_", "proactive_", "baseline_", "sdk_service")):
            return "W3"
        return "W2"
    if path.startswith(("src/gateway/", "src/pkg/", "src/services/", "src/messaging/", "src/llm/")):
        return "W1"
    if path.startswith("src/rag/"):
        return "W3"
    if path.startswith("src/init/") or path.startswith("src/execution/"):
        return "W3"
    if path.startswith("src/prober/") or path.startswith("src/anomaly/"):
        return "W2"
    if path.startswith("src/ingest/"):
        return "W2"
    return "W2"


def _parse_term_missing(stdout: str) -> list[FileCov]:
    rows: list[FileCov] = []
    for line in stdout.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        path, stmts, miss, pct = m.group(1), int(m.group(2)), int(m.group(3)), float(m.group(4))
        rows.append(FileCov(path=path, statements=stmts, missing=miss, pct=pct))
    return rows


def _aggregate(rows: list[FileCov], wave: str | None) -> tuple[int, int, float]:
    st = mi = 0
    for r in rows:
        if wave is not None and _wave_for(r.path) != wave:
            continue
        if wave is None and _wave_for(r.path) == "WX":
            continue
        st += r.statements
        mi += r.missing
    if st == 0:
        return 0, 0, 0.0
    return st, mi, 100.0 * (st - mi) / st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=35)
    ap.add_argument("--no-run", action="store_true", help="read pytest output from stdin instead of running")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    if args.no_run:
        text = sys.stdin.read()
    else:
        cmd = [
            str(root / ".venv/bin/python"),
            "-m",
            "pytest",
            "tests/",
            "-q",
            "--ignore=tests/integration",
            "--ignore=tests/real_services",
            "--cov=src",
            "--cov-report=term-missing",
        ]
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        text = proc.stdout + "\n" + proc.stderr
        if proc.returncode not in (0, 1):
            print(text[-4000:], file=sys.stderr)
            return proc.returncode

    rows = _parse_term_missing(text)
    if not rows:
        print("No coverage rows parsed. Paste pytest --cov output or fix regex.", file=sys.stderr)
        return 2

    rows_nx = [r for r in rows if _wave_for(r.path) != "WX"]
    total_st = sum(r.statements for r in rows_nx)
    total_mi = sum(r.missing for r in rows_nx)
    total_pct = 100.0 * (total_st - total_mi) / total_st if total_st else 0.0

    print("=== Omni src/ coverage (excluding WX tooling sample trees) ===")
    print(f"TOTAL  stmts={total_st}  miss={total_mi}  cov={total_pct:.2f}%")
    print()
    for w in ("W1", "W2", "W3"):
        st, mi, pct = _aggregate(rows, w)
        print(f"{w}  stmts={st}  miss={mi}  cov={pct:.2f}%")
    print()
    print(f"=== Top {args.top} files by missed lines (excluding WX) ===")
    for r in sorted((x for x in rows if _wave_for(x.path) != "WX"), key=lambda x: -x.missing)[: args.top]:
        print(f"{r.missing:4d}  {_wave_for(r.path):3s}  {r.pct:5.1f}%  {r.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
