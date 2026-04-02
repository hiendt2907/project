"""Zero-trust: Gateway chỉ FastAPI + Redis + Kafka — cấm import workers/reasoning/executor."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY_DIR = _ROOT / "src" / "gateway"
_FORBIDDEN = re.compile(
    r"(?:^|\s)(?:from|import)\s+(?:workers\.|pkg\.reasoning|pkg\.executor|src\.workers)",
    re.MULTILINE,
)


def _py_files() -> list[Path]:
    return sorted(_GATEWAY_DIR.glob("*.py"))


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_gateway_tree_has_no_worker_or_pkg_imports(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not _FORBIDDEN.search(text), f"Forbidden import boundary in {path}: { _FORBIDDEN.search(text).group(0)!r}"
