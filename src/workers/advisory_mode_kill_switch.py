"""Kill-Switch Enforcement for Advisory Mode — prevents all autonomous mutations."""

from __future__ import annotations

import logging
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.signer import AuditLedgerError

logger = logging.getLogger(__name__)


class AdvisoryModeKillSwitch:
    """
    Kill-switch enforcement that prevents autonomous mutations when disabled.
    Reads from Pydantic settings with strict fallback to False (fail-closed).
    If an LLM somehow hallucinates a mutation or an old code path tries to execute one,
    this module catches it and routes it to Telegram as a "Suggested Action" instead.
    """

    @staticmethod
    def validate_execution_gate(
        tool_name: str,
        args: dict[str, Any],
        context: str = "unknown",
        auto_execute_enabled: bool = False,
        siem_suggest_only: bool = True,
        *,
        tier: str | None = None,
        risk_override: str | None = None,
    ) -> tuple[bool, str]:
        """
        Pre-execution validation. Returns (allow_execute, reason).

        Args:
            tool_name: The mutation tool name (e.g., k8s_rollout_restart)
            args: Tool arguments
            context: Where the execution was requested (advisory_analyst, planner, etc.)
            auto_execute_enabled: OMNI_AUTO_EXECUTE_ENABLED from settings (default: False, fail-closed)
            siem_suggest_only: OMNI_SIEM_SUGGEST_ONLY from settings (default: True)
            tier: Autonomy tier (shadow|assist|auto). Khi set → áp ma trận tier×risk
                (MASTER_PLAN §3). None → giữ NGUYÊN hành vi legacy (tương thích ngược).
            risk_override: risk_class override đã resolve (DB→cache), nếu có.

        Returns:
            (allow_execute, reason). reason mang prefix TIER_GATE:<DECISION> khi dùng tier.
        """
        # ── Tier matrix path (MASTER_PLAN §3) ──────────────────────────────
        if tier is not None:
            from workers.tier_gate import ALLOW, HITL, gate_decision_for_tool

            decision, risk = gate_decision_for_tool(
                tool_name, tier=tier, override=risk_override
            )
            reason = f"TIER_GATE:{decision} tool={tool_name} tier={tier} risk={risk}"
            if decision == ALLOW:
                logger.info("event=tier_gate_allow tool=%s tier=%s risk=%s", tool_name, tier, risk)
                return True, reason
            logger.warning(
                "event=tier_gate_block tool=%s tier=%s risk=%s decision=%s",
                tool_name, tier, risk, decision,
            )
            # SUGGEST hoặc HITL → không tự chạy
            return False, reason

        # ── Legacy path (tier=None) — giữ nguyên hành vi cũ ────────────────
        if not auto_execute_enabled:
            reason = (
                f"ADVISORY_MODE_KILL_SWITCH: Mutation '{tool_name}' blocked. "
                f"Advisory Mode only supports read-only analysis. "
                f"(context={context})"
            )
            logger.warning(
                "event=kill_switch_blocked tool=%s context=%s reason=%s",
                tool_name,
                context,
                reason,
            )
            return False, reason

        # If somehow auto-execution is enabled (shouldn't happen), still block if tool is "dangerous"
        dangerous_tools = {
            "k8s_delete_pod",
            "k8s_delete_deployment",
            "k8s_delete_pvc",
            "k8s_patch_rbac",
            "k8s_patch_secret",
            "k8s_mutate_taint",
        }
        if tool_name in dangerous_tools:
            reason = f"SAFETY_GATE: '{tool_name}' is classified as dangerous; requires HITL approval."
            logger.warning(
                "event=safety_gate_blocked tool=%s reason=%s",
                tool_name,
                reason,
            )
            return False, reason

        return True, "execution_allowed"

    @staticmethod
    async def trap_hallucinated_mutation(
        tool_name: str,
        args: dict[str, Any],
        ctx: Any,
        trace: str,
        auto_execute_enabled: bool = False,
    ) -> str:
        """
        If a mutation slips through (LLM hallucination, old code path),
        trap it and emit a Telegram message with "Suggested Action" instead of executing.

        Args:
            auto_execute_enabled: OMNI_AUTO_EXECUTE_ENABLED from settings (passed for audit trail)

        Returns:
            Safe message for the user explaining what was blocked.
        """
        logger.critical(
            "event=hallucinated_mutation_trapped tool=%s trace=%s",
            tool_name,
            trace,
        )

        # CRAT: fail-closed — audit trapped mutation BEFORE any Telegram emit.
        _redis = getattr(ctx, "redis", None)
        _kafka = getattr(ctx, "kafka", None)
        _settings = getattr(ctx, "settings", None)
        _audit_topic = getattr(_settings, "kafka_topic_audit_chain", "omni-audit-chain")
        if _redis is not None:
            try:
                await write_audit_block(
                    event_type="MUTATION_TRAPPED",
                    trace_id=trace,
                    payload={"tool_name": tool_name, "args": args, "auto_execute_enabled": auto_execute_enabled},
                    redis=_redis,
                    kafka=_kafka,
                    kafka_topic=_audit_topic,
                )
            except AuditLedgerError as _audit_err:
                logger.critical(
                    "event=audit_chain_write_failed phase=trap_hallucinated_mutation trace=%s err=%s FAIL_CLOSED",
                    trace,
                    _audit_err,
                )
                return (
                    f"AUDIT_CHAIN_FAILURE: Cannot safely trap mutation '{tool_name}'. "
                    f"Audit write failed — transaction aborted. Trace: {trace}"
                )

        # Route to Telegram as advisory
        if hasattr(ctx, "telegram") and ctx.telegram:
            chat_id = getattr(ctx, "_current_chat_id", None)
            if chat_id:
                try:
                    suggestion_msg = (
                        f"🔒 *Suggested Action (NOT EXECUTED):*\n"
                        f"Tool: `{tool_name}`\n"
                        f"Args: `{args}`\n"
                        f"\n"
                        f"⚠️ This action was recommended by the Analyst but NOT automatically executed "
                        f"(Advisory Mode). Please review and execute manually if safe.\n"
                        f"Trace: `{trace}`"
                    )
                    await ctx.telegram.send_message(chat_id, suggestion_msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error("event=telegram_trap_send_error err=%r", e)

        return (
            f"ADVISED_ACTION: {tool_name} recommended but NOT executed in Advisory Mode. "
            f"Trace: {trace}. Operator must review and approve before execution."
        )

    @staticmethod
    def validate_advisor_output(advisory_dict: dict[str, Any]) -> tuple[bool, str]:
        """
        Post-output validation: ensure the Advisory object has no mutations embedded.

        Args:
            advisory_dict: Parsed AnalystAdvisory

        Returns:
            (valid, reason)
        """
        # Check proposed_remediation for any mutation-like action strings
        proposed_steps = advisory_dict.get("proposed_remediation", [])
        forbidden_keywords = {
            "kubectl delete",
            "kubectl drain",
            "kubectl taint",
            "rm -rf",
            "DROP TABLE",
            "DELETE FROM",
        }

        for step in proposed_steps:
            if not isinstance(step, dict):
                continue
            action = (step.get("action") or "").lower()
            for kw in forbidden_keywords:
                if kw.lower() in action:
                    reason = (
                        f"ADVISORY_VALIDATION: Proposed remediation step contains forbidden keyword '{kw}'. "
                        f"Action was: {step.get('action')}"
                    )
                    logger.error("event=advisory_validation_failed reason=%s", reason)
                    return False, reason

        return True, "advisory_valid"
