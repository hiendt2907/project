"""Autonomy contracts: alert lifecycle phases, GIGO metadata, context budgets, strict LLM JSON."""

from pkg.autonomy.gigo import build_gigo_metadata
from pkg.autonomy.lifecycle import AlertPhase, transition_to_alert_phase
from pkg.autonomy.llm_contract import (
    STRICT_REMEDIATION_JSON_SCHEMA,
    HighLevelRemediationPlan,
    map_high_level_plan_to_mutate,
    parse_high_level_plan_json,
)
from pkg.autonomy.transform import clamp_evidence_text, llm_evidence_char_budget

__all__ = [
    "AlertPhase",
    "STRICT_REMEDIATION_JSON_SCHEMA",
    "HighLevelRemediationPlan",
    "build_gigo_metadata",
    "clamp_evidence_text",
    "map_high_level_plan_to_mutate",
    "llm_evidence_char_budget",
    "parse_high_level_plan_json",
    "transition_to_alert_phase",
]
