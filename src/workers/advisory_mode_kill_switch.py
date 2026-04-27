"""Kill-Switch Enforcement for Advisory Mode — prevents all autonomous mutations."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AdvisoryModeKillSwitch:
    """
    Hardcoded kill-switch that prevents mutations in Advisory Mode.
    If an LLM somehow hallucinates a mutation or an old code path tries to execute one,
    this module catches it and routes it to Telegram as a "Suggested Action" instead.
    """

    # Hardcoded: Advisory Mode is ALWAYS read-only
    OMNI_AUTO_EXECUTE_ENABLED = False
    OMNI_SIEM_SUGGEST_ONLY = True

    @staticmethod
    def validate_execution_gate(
        tool_name: str,
        args: dict[str, Any],
        context: str = "unknown",
    ) -> tuple[bool, str]:
        """
        Pre-execution validation. Returns (allow_execute, reason).

        Args:
            tool_name: The mutation tool name (e.g., k8s_rollout_restart)
            args: Tool arguments
            context: Where the execution was requested (advisory_analyst, planner, etc.)

        Returns:
            (allow_execute=False, reason_message) — ALWAYS blocks in Advisory Mode
        """
        if not AdvisoryModeKillSwitch.OMNI_AUTO_EXECUTE_ENABLED:
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
    ) -> str:
        """
        If a mutation slips through (LLM hallucination, old code path),
        trap it and emit a Telegram message with "Suggested Action" instead of executing.

        Returns:
            Safe message for the user explaining what was blocked.
        """
        logger.critical(
            "event=hallucinated_mutation_trapped tool=%s trace=%s",
            tool_name,
            trace,
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
                    await ctx.telegram.send_message(chat_id, suggestion_msg, parse_mode="markdown")
                except Exception as e:
                    logger.warning("event=telegram_trap_send_error err=%s", e)

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
