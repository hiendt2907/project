#!/usr/bin/env python3
"""Gate: required phase report/review and project memory docs must exist."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    required = []
    for i in range(1, 6):
        required.append(ROOT / "docs" / "reports" / f"phase-{i}-report.md")
        required.append(ROOT / "docs" / "reports" / f"phase-{i}-review.md")
    required.append(ROOT / "docs" / "reports" / "project-memory.md")
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    if missing:
        print("FAIL: missing required docs:", file=sys.stderr)
        for m in missing:
            print(f" - {m}", file=sys.stderr)
        return 1
    print("OK: phase docs gate satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
