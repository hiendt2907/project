"""Executor-side: EXECUTE_MUTATE — chỉ tool Kubernetes (SDK + kubectl argv), không mở echo/shell/Prom/Redis."""

from __future__ import annotations

import json
import logging
from typing import Any

from pkg.autonomous_actions import build_action_feedback_body, infer_exit_code_from_tool_output
from pkg.executor.mutate_governance import (
    MUTATING_POLICY_GUARD_TOOLS as _MUTATING_POLICY_GUARD_TOOLS,
    governance_check_executor_mutate,
)
from pkg.executor.blast_radius import BLAST_SCORED_TOOLS as _BLAST_SCORED_TOOLS
from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
from pkg.risk_taxonomy import (
    K8S_SDK_MUTATING_TOOL_NAMES,
    K8S_SDK_READONLY_TOOL_NAMES,
    MUTATE_TOOL_ALLOWLIST,
    MUTATE_TOOL_REGISTRY_NAME,
    READONLY_TOOL_ALLOWLIST,
)
from services.audit_ledger.crat_event_types import CRAT_EVENT_DECISION_RENDERED
from services.audit_ledger.signer import AuditLedgerError
from workers.k8s_tools import deployment_evidence_snapshot, execute_rollout_restart_from_pending
from workers.rollback_executor import capture_pre_mutate_snapshot, snapshot_required
from workers.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

_FEEDBACK_TRUNC = 6000
_REASON_CODE_MARKER = "reason_code="


def _trunc_feedback_text(s: str, max_len: int = _FEEDBACK_TRUNC) -> str:
    s = s or ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "\n[...truncated]"


__all__ = [
    "K8S_SDK_MUTATING_TOOL_NAMES",
    "K8S_SDK_READONLY_TOOL_NAMES",
    "MUTATE_TOOL_ALLOWLIST",
    "MUTATE_TOOL_REGISTRY_NAME",
    "READONLY_TOOL_ALLOWLIST",
]


def _normalize_mutate_args_for_registry(reg_name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
    args = dict(raw_args or {})
    if reg_name != "k8s_describe_resource":
        return args
    if not args.get("resource_type"):
        kind = str(args.get("kind") or args.get("type") or "").strip().lower()
        mapping = {
            "pod": "Pod",
            "pods": "Pod",
            "deployment": "Deployment",
            "deploy": "Deployment",
            "service": "Service",
            "svc": "Service",
        }
        rt = mapping.get(kind)
        if rt:
            args["resource_type"] = rt
    # Planner sometimes emits pod/deployment/service fields instead of generic name.
    if not args.get("name"):
        for k in ("name", "pod", "deployment", "service"):
            v = str(args.get(k) or "").strip()
            if v:
                args["name"] = v
                break
    return args


def _reason_code_from_output(output: str) -> str | None:
    """Extract reason_code=XXX from a [DIAGNOSIS] deny message, if present."""
    idx = output.find(_REASON_CODE_MARKER)
    if idx < 0:
        return None
    rest = output[idx + len(_REASON_CODE_MARKER):]
    return rest.split()[0].strip() if rest.strip() else None


async def _emit_decision_rendered(
    ctx: Any, *, trace_id: str, tool_name: str, args: dict[str, Any], output: str, exit_code: int,
) -> None:
    """WS2 Decision Transparency Layer — 1 CRAT record per EXECUTE_MUTATE decision,
    explaining ALLOW vs DENY (and why) after run_execute_mutate_tool has already
    decided. Best-effort: a write failure here must not undo a decision already
    made (unlike MUTATION_ENQUEUED, which fail-closes because it gates dispatch).
    """
    redis = getattr(ctx, "redis", None)
    kafka = getattr(ctx, "kafka", None)
    settings = getattr(ctx, "settings", None)
    if redis is None:
        return
    reason_code = _reason_code_from_output(output)
    audit_topic = getattr(settings, "kafka_topic_audit_chain", "omni-audit-chain")
    try:
        from services.audit_ledger.chain_writer import write_audit_block

        await write_audit_block(
            event_type=CRAT_EVENT_DECISION_RENDERED,
            trace_id=trace_id,
            payload={
                "tool_name": tool_name,
                "allow": reason_code is None,
                "reason_code": reason_code,
                "exit_code": exit_code,
            },
            redis=redis,
            kafka=kafka,
            kafka_topic=audit_topic,
        )
    except AuditLedgerError as exc:
        logger.warning(
            "event=decision_rendered_audit_skip trace=%s tool=%s err=%s",
            trace_id, tool_name, exc,
        )


async def run_execute_mutate_tool(
    ctx: Any,
    *,
    tool_name: str,
    args: dict[str, Any],
    trace_id: str,
) -> tuple[str, int]:
    """Returns (combined_output, exit_code). Wraps _run_execute_mutate_tool_impl
    with 1 CRAT DECISION_RENDERED record (WS2 Decision Transparency Layer)."""
    output, exit_code = await _run_execute_mutate_tool_impl(
        ctx, tool_name=tool_name, args=args, trace_id=trace_id,
    )
    await _emit_decision_rendered(
        ctx, trace_id=trace_id, tool_name=tool_name, args=args or {},
        output=output, exit_code=exit_code,
    )
    return output, exit_code


async def _run_execute_mutate_tool_impl(
    ctx: Any,
    *,
    tool_name: str,
    args: dict[str, Any],
    trace_id: str,
) -> tuple[str, int]:
    """Returns (combined_output, exit_code)."""
    name = str(tool_name or "").strip()
    reg_name = MUTATE_TOOL_REGISTRY_NAME.get(name, name)
    ws = getattr(ctx, "settings", None)
    force_nsenter = bool(getattr(ws, "omni_executor_force_nsenter", False)) if ws is not None else False
    if force_nsenter and reg_name != "kubectl_cluster":
        msg = (
            "[DATA] error\n[DIAGNOSIS] reason_code="
            f"{ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"executor_policy_denied: tool {name!r} resolved={reg_name!r} "
            "must route through kubectl_cluster with nsenter host wrapper."
        )
        return msg, 1
    gov_ok, gov_msg = governance_check_executor_mutate(
        settings=ws,
        resolved_tool_name=reg_name,
        args=args,
    )
    if not gov_ok:
        return gov_msg, infer_exit_code_from_tool_output(gov_msg)

    # Blast-Radius Diff-Scoring + Impact-Tree (plan step 3). Code-hard safety net that a
    # green dry-run cannot wave through: scores pods destroyed/restarted via the K8s
    # dependency graph + GC cascade. Opt-in (lab env), guarded so a read failure never
    # blocks a legit small mutate.
    if bool(getattr(ws, "omni_blast_radius_enabled", False)) and reg_name in _BLAST_SCORED_TOOLS:
        try:
            from pkg.executor.blast_radius import K8sBlastReader, assess_blast_radius

            try:
                _reader = K8sBlastReader()
            except Exception as _re:
                _reader = None
                logger.warning("[%s] blast_radius reader unavailable: %s", trace_id, _re)
            # Always call assess_blast_radius, even with reader=None: it has its own
            # fail-closed handling for destructive verbs when there's no cluster view
            # (e.g. circuit breaker open after repeated K8s API failures). Skipping the
            # call here would silently disable that fail-closed path.
            _verdict = await assess_blast_radius(
                _reader, tool=reg_name, args=args or {},
                max_pods=int(getattr(ws, "omni_blast_max_pods", 10) or 10),
                capacity_drop_pct=float(getattr(ws, "omni_blast_capacity_drop_pct", 20.0) or 20.0),
            )
            if _verdict.hard_block:
                logger.error(
                    "[%s] event=blast_radius_hard_block tool=%s affected=%d reasons=%s",
                    trace_id, reg_name, _verdict.affected_pods, "; ".join(_verdict.reasons),
                )
                msg = _verdict.deny_message()
                return msg, infer_exit_code_from_tool_output(msg)
        except Exception as _be:
            logger.warning("[%s] blast_radius assess error (allowing): %s", trace_id, _be)

    # S1.2: Capture pre-mutate snapshot for rollback if tool modifies state.
    if bool(getattr(ws, "omni_auto_rollback_enabled", True)) and snapshot_required(reg_name):
        ttl = int(getattr(ws, "omni_rollback_snapshot_ttl_sec", 3600) or 3600)
        # Store target name on ctx so rollback_executor can reconstruct it.
        _target_name = str((args or {}).get("name") or (args or {}).get("deployment") or "")
        ctx.rollback_target_name = _target_name  # type: ignore[attr-defined]
        await capture_pre_mutate_snapshot(ctx, reg_name, args or {}, trace_id, ttl_sec=ttl)

    unrestricted = bool(getattr(ws, "omni_unrestricted_tool_execution", False))
    if unrestricted:
        # Even in unrestricted mode, restrict to the approved mutate allowlist.
        if reg_name not in MUTATE_TOOL_ALLOWLIST:
            msg = (
                f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
                f"tool={name!r} resolved={reg_name!r} not in mutate allowlist (unrestricted mode still bounded)."
            )
            return msg, 1
        fn_any = TOOL_REGISTRY.get(reg_name) or TOOL_REGISTRY.get(name)
        if fn_any is None:
            return f"[DATA] error\n[DIAGNOSIS] Unknown tool {name!r} resolved={reg_name!r}", 1
        try:
            run_name = reg_name if TOOL_REGISTRY.get(reg_name) is not None else name
            run_args = _normalize_mutate_args_for_registry(run_name, args)
            out = await fn_any(ctx, run_args)
            s = str(out)
            return s, infer_exit_code_from_tool_output(s)
        except Exception as e:
            logger.exception("unrestricted_execute tool %s: %s", name, e)
            return f"[DATA] error\n[DIAGNOSIS] {e!s}", 1
    if reg_name in READONLY_TOOL_ALLOWLIST:
        msg = (
            f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"read_only_tool_blocked={name!r} resolved={reg_name!r} channel=EXECUTE_MUTATE"
        )
        return msg, 1
    if reg_name not in K8S_SDK_MUTATING_TOOL_NAMES:
        msg = (
            f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"tool={name!r} resolved={reg_name!r} not in mutate-only allowlist."
        )
        return msg, 1
    if reg_name not in TOOL_REGISTRY:
        msg = (
            f"[DATA] error\n[DIAGNOSIS] reason_code={ERR_GOV_UNAUTHORIZED_MUTATION} "
            f"Kubernetes tool {reg_name!r} missing from TOOL_REGISTRY "
            f"(from {name!r}) — deployment bug."
        )
        return msg, 1
    if name == "k8s_rollout_restart":
        ns = str((args or {}).get("namespace") or "").strip()
        dep = str((args or {}).get("deployment") or (args or {}).get("name") or "").strip()
        if not ns or not dep:
            return "[DATA] error\n[DIAGNOSIS] k8s_rollout_restart requires namespace and deployment in args.", 1
        snap = (args or {}).get("evidence_snapshot")
        if not isinstance(snap, dict) or snap.get("deployment_generation") is None:
            snap = await deployment_evidence_snapshot(ns, dep)
        data: dict[str, Any] = {
            "kind": "k8s_rollout_restart",
            "namespace": ns,
            "deployment": dep,
            "trace_id": trace_id,
            "evidence_snapshot": snap,
        }
        out = await execute_rollout_restart_from_pending(ctx, data)
        return out, infer_exit_code_from_tool_output(out)

    fn = TOOL_REGISTRY.get(reg_name)
    if fn is None:
        return f"[DATA] error\n[DIAGNOSIS] Unknown tool {reg_name!r} (from {name!r})", 1
    try:
        norm_args = _normalize_mutate_args_for_registry(reg_name, args)
        out = await fn(ctx, norm_args)
        s = str(out)
        return s, infer_exit_code_from_tool_output(s)
    except Exception as e:
        logger.exception("autonomous_execute tool %s: %s", name, e)
        return f"[DATA] error\n[DIAGNOSIS] {e!s}", 1


async def publish_action_feedback(
    ctx: Any,
    *,
    trace_id: str,
    tool_name: str,
    correlation_id: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    status: str = "ok",
    skipped_reason: str | None = None,
    mutate_args: dict[str, Any] | None = None,
) -> None:
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    if k is None or ws is None:
        return
    topic = getattr(ws, "kafka_topic_action_feedback", "omni-action-feedback")
    out_s = _trunc_feedback_text(stdout) if exit_code != 0 else (stdout or "")
    err_s = _trunc_feedback_text(stderr) if exit_code != 0 else (stderr or "")
    body = build_action_feedback_body(
        trace_id=trace_id,
        tool_name=tool_name,
        correlation_id=correlation_id,
        stdout=out_s,
        stderr=err_s,
        exit_code=exit_code,
        status=status,
        skipped_reason=skipped_reason,
        mutate_args=mutate_args,
    )
    try:
        await k.send_dict(topic, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_feedback_published trace=%s exit_code=%s status=%s",
            trace_id,
            exit_code,
            status,
        )
    except Exception as e:
        logger.warning("action_feedback publish failed: %s", e)
