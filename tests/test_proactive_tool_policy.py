"""proactive_tool_policy: phase tool names must exist in TOOL_REGISTRY."""

from __future__ import annotations

from workers.proactive_guardrails import PROACTIVE_MUTATE_TOOLS
from workers.proactive_tool_policy import PROACTIVE_DIAGNOSE_TOOLS, PROACTIVE_RECHECK_TOOLS
from workers.tools import TOOL_REGISTRY


def test_proactive_diagnose_tools_registered() -> None:
    missing = sorted(PROACTIVE_DIAGNOSE_TOOLS - TOOL_REGISTRY.keys())
    assert not missing, f"diagnose tools not in TOOL_REGISTRY: {missing}"


def test_proactive_recheck_tools_registered() -> None:
    missing = sorted(PROACTIVE_RECHECK_TOOLS - TOOL_REGISTRY.keys())
    assert not missing, f"recheck tools not in TOOL_REGISTRY: {missing}"


def test_proactive_mutate_tools_registered() -> None:
    missing = sorted(PROACTIVE_MUTATE_TOOLS - TOOL_REGISTRY.keys())
    assert not missing, f"mutate tools not in TOOL_REGISTRY: {missing}"


def test_recheck_subset_of_diagnose() -> None:
    """Recheck uses read-only style tools; keep them valid diagnose names."""
    assert PROACTIVE_RECHECK_TOOLS <= PROACTIVE_DIAGNOSE_TOOLS
