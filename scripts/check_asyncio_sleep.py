#!/usr/bin/env python3
"""CI gate: detect time.sleep() calls inside async functions.

Usage: python scripts/check_asyncio_sleep.py [paths...]
Exit 1 if any violations found.
"""
import ast
import sys
from pathlib import Path


def _is_time_sleep_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr == "sleep"
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "time"
    )


def _walk_async_body(
    nodes: list[ast.stmt],
    fn_name: str,
    violations: list[tuple[int, str]],
) -> None:
    """Walk statement list, descending into control flow but NOT into nested function defs."""
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Nested function def: recurse only if async (new async context)
            if isinstance(node, ast.AsyncFunctionDef):
                _walk_async_body(node.body, node.name, violations)
            # Nested sync def: skip entirely — time.sleep is fine there
            continue
        if isinstance(node, ast.Expr) and _is_time_sleep_call(node.value):
            violations.append((node.lineno, fn_name))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, list):
                continue
            if isinstance(child, ast.stmt):
                _walk_async_body([child], fn_name, violations)


def _check_file(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        # Only check top-level async defs (not nested ones — they'll be visited too)
        _walk_async_body(node.body, node.name, violations)

    # Deduplicate (nested async defs visited twice via ast.walk)
    return sorted(set(violations))





def main() -> int:
    roots = [Path(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else [Path("src")]
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))

    found = False
    for path in sorted(files):
        for lineno, fn_name in _check_file(path):
            print(f"ASYNCIO_SLEEP_VIOLATION {path}:{lineno} in async def {fn_name}()")
            found = True

    if found:
        print("\nFAIL: Replace time.sleep() with await asyncio.sleep() in async functions.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
