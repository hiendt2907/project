"""Prompt strings: backtick tool names must match TOOL_REGISTRY (chống drift)."""

from __future__ import annotations

import re

import pytest

from workers.handlers import _k8s_smart_target_hint
from workers.tools import TOOL_REGISTRY

_BACKTICK_ID = re.compile(r"`([a-z][a-z0-9_]*)`")
# Không phải tool — chỉ là nhãn trong prose / ví dụ JSON field
_NOT_TOOLS = frozenset(
    {
        "learned_infra",
        "topology_cache",
        "text",
        "tool",
        "command",
        "layer",
    }
)
_ALIAS_OK = frozenset({"query_victoria_metrics"})


def _tokens(*blobs: str) -> set[str]:
    out: set[str] = set()
    for b in blobs:
        out.update(_BACKTICK_ID.findall(b))
    return out


@pytest.mark.parametrize(
    "blob_name,module",
    [
        ("SLOW_SYSTEM_VI", "handlers"),
        ("SLOW_SYSTEM_GOD_VI", "handlers"),
        ("K8S_TOOL_GUIDANCE_VI", "handlers"),
        ("SLOW_SYSTEM_EN", "ollama_prompts_en"),
        ("SLOW_SYSTEM_GOD_EN", "ollama_prompts_en"),
        ("K8S_TOOL_GUIDANCE_EN", "ollama_prompts_en"),
    ],
)
def test_slow_system_backticks_are_registered_or_alias(blob_name: str, module: str) -> None:
    import importlib

    mod = importlib.import_module(f"workers.{module}")
    blob = getattr(mod, blob_name)
    for tok in _tokens(blob):
        if tok in _NOT_TOOLS:
            continue
        assert tok in TOOL_REGISTRY or tok in _ALIAS_OK, f"{blob_name}: `{tok}` không có trong TOOL_REGISTRY"


def test_k8s_smart_target_hint_tools_registered() -> None:
    h = _k8s_smart_target_hint("check pod nginx rollout deployment") or ""
    for tok in _tokens(h):
        if tok in _NOT_TOOLS:
            continue
        assert tok in TOOL_REGISTRY or tok in _ALIAS_OK, f"hint: `{tok}`"
