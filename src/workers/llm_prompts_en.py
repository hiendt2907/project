"""English prompts for all LLM chat calls (worker).

Moved to pkg/reasoning/llm_prompts_en.py (WS1, dependency-direction fix). Re-exported
here unchanged so existing worker callers are unaffected.
"""

from __future__ import annotations

from pkg.reasoning.llm_prompts_en import (
    AGENTIC_LAB_SHELL_SUPPLEMENT_UNATTENDED_EN,
    AGENTIC_REACT_RULES_EN,
    AGENTIC_REACT_RULES_UNATTENDED_SUPPLEMENT_EN,
    CONV_FALLBACK_SYSTEM_EN,
    FINAL_FORMAT_EN,
    K8S_TOOL_GUIDANCE_EN,
    LLM_MAX_OUTPUT_WORDS,
    SLOW_SYSTEM_EN,
    SLOW_SYSTEM_GOD_EN,
    SLOW_SYSTEM_GOD_UNATTENDED_EN,
    SLOW_SYSTEM_UNATTENDED_EN,
    SRE_JSON_GENERATOR_EN,
    SRE_JSON_GENERATOR_UNATTENDED_EN,
    TOOL_CATALOG_PLACEHOLDER,
    VENDOR_KNOWLEDGE_GUIDANCE_EN,
    slow_system_body_for_unattended_alert_en,
    truncate_plain_text_to_max_words,
)

__all__ = [
    "AGENTIC_LAB_SHELL_SUPPLEMENT_UNATTENDED_EN",
    "AGENTIC_REACT_RULES_EN",
    "AGENTIC_REACT_RULES_UNATTENDED_SUPPLEMENT_EN",
    "CONV_FALLBACK_SYSTEM_EN",
    "FINAL_FORMAT_EN",
    "K8S_TOOL_GUIDANCE_EN",
    "LLM_MAX_OUTPUT_WORDS",
    "SLOW_SYSTEM_EN",
    "SLOW_SYSTEM_GOD_EN",
    "SLOW_SYSTEM_GOD_UNATTENDED_EN",
    "SLOW_SYSTEM_UNATTENDED_EN",
    "SRE_JSON_GENERATOR_EN",
    "SRE_JSON_GENERATOR_UNATTENDED_EN",
    "TOOL_CATALOG_PLACEHOLDER",
    "VENDOR_KNOWLEDGE_GUIDANCE_EN",
    "slow_system_body_for_unattended_alert_en",
    "truncate_plain_text_to_max_words",
]
