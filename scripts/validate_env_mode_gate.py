#!/usr/bin/env python3
"""Static gate: OMNI_ENV_MODE contract must default to prod."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "src" / "workers" / "settings.py"
CONFIGMAP = ROOT / "k8s" / "deployments" / "omni-worker-configmap.yaml"


def _contains(path: Path, token: str) -> bool:
    try:
        return token in path.read_text(encoding="utf-8")
    except Exception:
        return False


def main() -> int:
    ok_settings = _contains(SETTINGS, 'env_mode: Literal["prod", "dev"] = Field(') and _contains(
        SETTINGS, 'default="prod"'
    )
    ok_cfg = _contains(CONFIGMAP, 'OMNI_ENV_MODE: "prod"')
    if not ok_settings:
        print("FAIL: WorkerSettings must define env_mode with default prod.", file=sys.stderr)
        return 1
    if not ok_cfg:
        print("FAIL: omni-worker-configmap must set OMNI_ENV_MODE=prod.", file=sys.stderr)
        return 1
    print("OK: env_mode gate satisfied (default prod).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
