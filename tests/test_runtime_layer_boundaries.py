"""Architecture guardrails for ADR-004 runtime convergence.

These tests intentionally inspect imports instead of importing the whole application. The
gateway image must be able to use pure AOIP/domain contracts without importing the worker
execution engine, Kafka, Kubernetes or LLM runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _imports_under(path: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for file in sorted(path.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend((file, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.append((file, node.module))
    return found


def test_gateway_never_imports_worker_execution_engine():
    violations = [
        (file, module)
        for file, module in _imports_under(ROOT / "src" / "gateway")
        if module == "workers" or module.startswith("workers.")
    ]
    assert violations == [], f"gateway must not import workers: {violations}"


def test_aoip_domain_never_imports_worker_execution_engine():
    violations = [
        (file, module)
        for file, module in _imports_under(ROOT / "src" / "aoip")
        if module == "workers" or module.startswith("workers.")
    ]
    assert violations == [], f"AOIP must not import workers: {violations}"


def test_command_protocol_is_dependency_light():
    forbidden_roots = {"aiokafka", "kubernetes", "redis", "httpx", "pydantic"}
    violations = []
    for file, module in _imports_under(ROOT / "src" / "aoip" / "protocol"):
        if module.split(".", 1)[0] in forbidden_roots:
            violations.append((file, module))
    assert violations == [], f"protocol must remain pure: {violations}"
