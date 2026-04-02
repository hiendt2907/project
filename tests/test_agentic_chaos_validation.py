"""Smoke: chaos script --help (không cần cluster)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_agentic_chaos_validation_help() -> None:
    r = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "agentic_chaos_validation.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0
    assert "intensity" in r.stdout
    assert "--skip-kubectl" in r.stdout
    assert "--redis-url" in r.stdout
