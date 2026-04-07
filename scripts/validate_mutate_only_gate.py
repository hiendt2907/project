#!/usr/bin/env python3
"""Static gate: EXECUTE_MUTATE must stay mutate-only."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST, READONLY_TOOL_ALLOWLIST  # noqa: E402


def main() -> int:
    overlap = sorted(set(MUTATE_TOOL_ALLOWLIST) & set(READONLY_TOOL_ALLOWLIST))
    if overlap:
        print(f"FAIL: mutate/read-only overlap detected: {overlap}", file=sys.stderr)
        return 1
    if "k8s_describe_resource" in MUTATE_TOOL_ALLOWLIST:
        print("FAIL: k8s_describe_resource must not be executable in EXECUTE_MUTATE.", file=sys.stderr)
        return 1
    print("OK: mutate-only gate satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
