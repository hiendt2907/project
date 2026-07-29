"""AutonomyGate — evaluates per-incident autonomy level with CRAT audit trail.

CRAT fail-closed invariant: write_audit_block() MUST succeed before any downstream
action. If it fails, AuditLedgerError is raised and the caller must abort.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.signer import AuditLedgerError

from pkg.autonomy.policy import (
    AutonomyLevel,
    AutonomyPolicyStore,
    PolicyRule,
    find_matching_rule,
)

logger = logging.getLogger(__name__)

_FP_ESCALATION_THRESHOLD = 0.15  # if fp_rate exceeds this, FULL_AUTO → SUGGEST_ONLY

_store = AutonomyPolicyStore()


class AutonomyGate:
    """Evaluate per-incident autonomy level against live policy with CRAT audit."""

    async def evaluate(
        self,
        lane: str,
        severity: str,
        action_type: str,
        fp_rate: float,
        ctx: Any,
        trace_id: str = "",
    ) -> AutonomyLevel:
        """Resolve the autonomy level for an (lane, severity, action_type) triple.

        Steps:
          1. Load policy from Redis (live — supports hot reload).
          2. Find first matching rule.
          3. If fp_rate > threshold and resolved == FULL_AUTO, escalate to SUGGEST_ONLY.
          4. Write AUTONOMY_DECISION to CRAT (fail-closed).
          5. Return resolved level.

        Raises AuditLedgerError if CRAT write fails (fail-closed).
        """
        redis = getattr(ctx, "redis", None)
        kafka = getattr(ctx, "kafka", None)
        settings = getattr(ctx, "settings", None)
        audit_topic = getattr(settings, "kafka_topic_audit_chain", "omni-audit-chain")

        # 1. Load live policy
        rules = await _store.get_policy(redis)

        # 2. First matching rule wins
        matched_rule: PolicyRule | None = find_matching_rule(rules, lane, severity, action_type)
        if matched_rule is None:
            # Should not happen — DEFAULT_POLICY has a catch-all — but be safe
            resolved_level = AutonomyLevel.SUGGEST_ONLY
            rule_desc = "no_match_fallback"
        else:
            resolved_level = matched_rule.level
            rule_desc = (
                f"lane={matched_rule.lane} severity={matched_rule.severity} "
                f"action_type={matched_rule.action_type}"
            )

        original_level = resolved_level

        # 3. fp_rate escalation: FULL_AUTO → SUGGEST_ONLY when FP rate is high
        #    HITL and ALERT_ONLY cannot be downgraded by fp_rate (security invariant)
        fp_escalated = False
        if resolved_level == AutonomyLevel.FULL_AUTO and fp_rate > _FP_ESCALATION_THRESHOLD:
            resolved_level = AutonomyLevel.SUGGEST_ONLY
            fp_escalated = True
            logger.warning(
                "autonomy_gate: fp_rate=%.3f > threshold=%.2f — escalated %s → %s trace=%s",
                fp_rate,
                _FP_ESCALATION_THRESHOLD,
                original_level,
                resolved_level,
                trace_id,
            )

        audit_payload: dict[str, Any] = {
            "lane": lane,
            "severity": severity,
            "action_type": action_type,
            "resolved_level": resolved_level.value,
            "original_level": original_level.value,
            "fp_rate": fp_rate,
            "fp_escalated": fp_escalated,
            "policy_rule_matched": rule_desc,
            "trace_id": trace_id,
            "evaluated_at": time.time(),
        }

        # 4. CRAT — fail-closed
        if redis is not None:
            try:
                await write_audit_block(
                    event_type="AUTONOMY_DECISION",
                    trace_id=trace_id,
                    payload=audit_payload,
                    redis=redis,
                    kafka=kafka,
                    kafka_topic=audit_topic,
                )
            except AuditLedgerError:
                logger.critical(
                    "autonomy_gate: CRAT write failed trace=%s — FAIL_CLOSED raising",
                    trace_id,
                )
                raise

        logger.info(
            "autonomy_gate: resolved lane=%s severity=%s action=%s level=%s fp_rate=%.3f trace=%s",
            lane,
            severity,
            action_type,
            resolved_level,
            fp_rate,
            trace_id,
        )
        return resolved_level

    async def get_fp_rate_for_lane(
        self, lane: str, redis: Any, *, tenant_id: str = "default"
    ) -> float:
        """False positive rate for *lane* over the last 24h, from the KPI z-sets.

        Reads via ``read_outcome_rates`` so the key shape matches what ``KPIStore``
        writes; the previous hand-built ``omni:kpi:z:false_positive`` keys omitted the
        tenant segment the writer uses and therefore always counted 0 samples.

        Returns 1.0 (worst case) when there is no data — "we have never measured this"
        must not be indistinguishable from "measured, zero false positives", or the
        gate silently opens on an unproven lane.
        """
        if redis is None:
            return 1.0
        try:
            from workers.kpi_metrics import read_outcome_rates

            rates = await read_outcome_rates(redis, tenant_id=tenant_id)
            # Denominator is accepted + false_positive only — `rejected` means the
            # operator declined the action, not that the detection was wrong, so it
            # does not belong in a false-POSITIVE rate. (Kept from the original
            # formula; only the key shape was wrong before.)
            fp_count = int(rates.get("false_positive") or 0)
            accepted = int(rates.get("accepted") or 0)
            denom = accepted + fp_count
            if denom == 0:
                logger.info(
                    "autonomy_gate: no KPI samples for lane=%s tenant=%s — "
                    "treating fp_rate as 1.0 (FAIL_CLOSED)",
                    lane, tenant_id,
                )
                return 1.0
            return fp_count / denom
        except Exception as exc:
            logger.warning("autonomy_gate: fp_rate read error lane=%s: %s", lane, exc)
            return 1.0
