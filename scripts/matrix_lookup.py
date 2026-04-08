#!/usr/bin/env python3
"""Lookup scenario metadata from merged matrix YAML path(s)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def matrix_paths() -> list[Path]:
    raw = os.environ.get("MATRIX_PATHS") or os.environ.get("MATRIX_FILE") or str(ROOT / "config" / "incident_training_matrix.yaml")
    parts: list[str] = []
    for sep in (":", ","):
        if sep in raw:
            parts = [p.strip() for p in raw.replace(sep, ",").split(",") if p.strip()]
            break
    if not parts:
        parts = [raw.strip()]
    out: list[Path] = []
    for p in parts:
        path = Path(p) if Path(p).is_absolute() else ROOT / p
        out.append(path)
    return out


def merged_scenarios() -> list[dict]:
    rows: list[dict] = []
    for path in matrix_paths():
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for row in data.get("scenarios") or []:
            if isinstance(row, dict) and str(row.get("id") or "").strip():
                rows.append(row)
    return rows


def runner_for(sid: str) -> str:
    for row in merged_scenarios():
        if str(row.get("id") or "").strip() == sid:
            return str(row.get("runner") or "")
    return ""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: matrix_lookup.py runner <scenario_id> | all-ids")
    cmd = sys.argv[1]
    if cmd == "runner":
        print(runner_for(sys.argv[2]))
        return
    if cmd == "all-ids":
        ids = [str(r.get("id") or "").strip() for r in merged_scenarios()]
        print(",".join(ids))
        return
    raise SystemExit("unknown command")


if __name__ == "__main__":
    main()
