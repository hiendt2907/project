#!/usr/bin/env python3
"""Static gate: advanced self-learning flags must remain safe-by-default."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "src" / "workers" / "settings.py"


def _contains(token: str) -> bool:
    try:
        return token in SETTINGS.read_text(encoding="utf-8")
    except Exception:
        return False


def main() -> int:
    checks = {
        "multi_hypothesis_enabled_default_off": _contains("multi_hypothesis_enabled: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_MULTI_HYPOTHESIS_ENABLED")')
        and _contains("default=False"),
        "multi_hypothesis_shadow_only_default_on": _contains("multi_hypothesis_shadow_only: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_MULTI_HYPOTHESIS_SHADOW_ONLY")')
        and _contains("default=True"),
        "deep_probe_orchestration_enabled_default_off": _contains("deep_probe_orchestration_enabled: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_DEEP_PROBE_ORCHESTRATION_ENABLED")'),
        "knowledge_draft_enabled_default_off": _contains("knowledge_draft_enabled: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_KNOWLEDGE_DRAFT_ENABLED")'),
        "shadow_influence_suggest_only_default_off": _contains("shadow_influence_suggest_only: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_SHADOW_INFLUENCE_SUGGEST_ONLY")'),
        "autonomous_writeback_enabled_default_off": _contains("autonomous_writeback_enabled: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_AUTONOMOUS_WRITEBACK_ENABLED")'),
        "knowledge_promotion_enabled_default_off": _contains("knowledge_promotion_enabled: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_KNOWLEDGE_PROMOTION_ENABLED")'),
        "autodoc_git_push_enabled_default_off": _contains("autodoc_git_push_enabled: bool = Field(")
        and _contains('validation_alias=AliasChoices("OMNI_AUTODOC_GIT_PUSH_ENABLED")'),
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        print("FAIL: non-impact guard defaults regressed", file=sys.stderr)
        for key in failed:
            print(f" - {key}")
        return 1
    print("OK: non-impact guard defaults are safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
