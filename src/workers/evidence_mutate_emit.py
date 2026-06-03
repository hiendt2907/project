"""Helpers to emit EXECUTE_MUTATE from evidence batch (namespace/deployment from alert labels)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from pkg.autonomous_actions import build_execute_mutate_body
from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.crat_event_types import (
    CRAT_EVENT_MUTATION_ENQUEUE_FAILED,
    CRAT_EVENT_MUTATION_ENQUEUED,
)
from services.audit_ledger.signer import AuditLedgerError
from workers.alert_to_event import anomaly_event_dict_from_evidence_batch
from workers.diagnostic_dispatcher import probe_ids_for_alertname
from pkg.reasoning.alert_identity import labels_dict_from_canonical_query_snippet, parse_omni_verify_required
from pkg.reasoning.incident_matrix_profile import alertname_from_batch

logger = logging.getLogger(__name__)


def _mutation_enqueue_audit_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Compact CRAT payload — no raw Secret values; args hashed."""
    trace_id = str(body.get("trace_id") or "")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    tool_name = str(data.get("tool_name") or "")
    attempt_count = int(data.get("attempt_count") or 1)
    correlation_id = str(data.get("correlation_id") or "")
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    args_digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "trace_id": trace_id,
        "tool_name": tool_name,
        "attempt_count": attempt_count,
        "correlation_id": correlation_id,
        "args_digest": args_digest,
    }


def _verify_probe_ids_from_batch(batch: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for b in batch:
        p = str(b.get("probe") or "").strip()
        if p and p not in seen:
            seen.append(p)
    if seen:
        return seen
    an = alertname_from_batch(batch)
    return probe_ids_for_alertname(an)


def _symptom_group_from_batch(batch: list[dict[str, Any]]) -> str:
    for b in batch:
        sg = str(b.get("symptom_group") or "").strip()
        if sg:
            return sg
    return ""

# Workload CPU / resource alerts — rollout gate sau RAG/diagnosis.
_RE_CPU_INCIDENT = re.compile(
    r"(highcpu|\bcpu\b|cpu\s+utilization|millicore|millicores|cgroup)",
    re.IGNORECASE,
)
_RE_FAULT_INCIDENT = re.compile(
    r"(createcontainer|crashloop|imagepull|probefail|readiness|liveness|backoff|oom|oomkilled|failedmount|unschedul)",
    re.IGNORECASE,
)


def _deployment_name_from_alert_labels(labels: dict[str, Any]) -> str:
    """Prometheus rules may use `deployment` or `workload` (chaos KubePodCrashLoopVictim uses workload)."""
    for k in ("deployment", "deployment_name", "workload"):
        v = str(labels.get(k) or "").strip()
        if v:
            return v
    return ""


def rollout_args_from_evidence_batch(batch: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort: namespace + deployment from canonical_query_snippet JSON (alert labels)."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
            labels = j.get("labels") if isinstance(j, dict) else None
            if not isinstance(labels, dict):
                continue
            ns = str(labels.get("namespace") or "").strip()
            dep = _deployment_name_from_alert_labels(labels)
            if ns and dep:
                return {"namespace": ns, "deployment": dep}
        except Exception:
            continue
    return None


def should_try_rollout_from_rag(suggested_tool: str, diag_snippet: str) -> bool:
    st = (suggested_tool or "").lower()
    if "rollout" in st or "restart" in st:
        return True
    d = (diag_snippet or "").lower()
    return "restart" in d or "rollout" in d


def workload_cpu_incident_rollout_eligible(batch: list[dict[str, Any]]) -> bool:
    """
    True when evidence describes a workload CPU incident (alert hint / labels).
    Caller runs after RAG/diagnosis path (SDK evidence already in batch JSON).
    """
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_CPU_INCIDENT.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if snip.startswith("{"):
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else None
                if isinstance(labels, dict):
                    an = str(labels.get("alertname") or "")
                    if "cpu" in an.lower():
                        return True
            except Exception:
                continue
    return False


def workload_fault_incident_rollout_eligible(batch: list[dict[str, Any]]) -> bool:
    """True when evidence points to a concrete workload fault where restart is a safe first mutate."""
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_FAULT_INCIDENT.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        labels = j.get("labels")
        if not isinstance(labels, dict):
            continue
        alertname = str(labels.get("alertname") or "")
        reason = str(labels.get("reason") or "")
        if _RE_FAULT_INCIDENT.search(alertname) or _RE_FAULT_INCIDENT.search(reason):
            return True
    return False


def should_emit_rollout_after_rag(
    *,
    suggested_tool: str,
    diag_snippet: str,
    batch: list[dict[str, Any]],
    rr: dict[str, Any] | None,
    autonomous_rollout_on_cpu_incident: bool,
    autonomous_rollout_on_fault_incident: bool = False,
) -> bool:
    """
    RAG often suggests read-only tools (e.g. kubectl_get_events) while the incident
    is still a real CPU spike on a named Deployment — emit rollout restart when enabled.
    """
    if should_try_rollout_from_rag(suggested_tool, diag_snippet):
        return True
    if not rr:
        return False
    if autonomous_rollout_on_cpu_incident and workload_cpu_incident_rollout_eligible(batch):
        return True
    if autonomous_rollout_on_fault_incident and workload_fault_incident_rollout_eligible(batch):
        return True
    return False


async def emit_execute_mutate(
    ctx: Any,
    *,
    trace: str,
    tool_name: str,
    args: dict[str, Any],
    attempt_count: int = 1,
    reasoning_chain: dict[str, Any] | None = None,
) -> bool:
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    r = getattr(ctx, "redis", None)
    if k is None or ws is None:
        logger.warning(
            "event=execute_mutate_emit_aborted trace=%s reason=missing_kafka_or_settings",
            trace,
        )
        return False
    if r is None:
        logger.warning(
            "event=execute_mutate_emit_aborted trace=%s reason=missing_redis_crat_requires_redis",
            trace,
        )
        return False
    body = build_execute_mutate_body(
        trace,
        tool_name=tool_name,
        args=args,
        attempt_count=attempt_count,
        reasoning_chain=reasoning_chain,
    )
    audit_topic = getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain")
    audit_payload = _mutation_enqueue_audit_payload(body)
    try:
        await write_audit_block(
            event_type=CRAT_EVENT_MUTATION_ENQUEUED,
            trace_id=trace,
            payload=audit_payload,
            redis=r,
            kafka=k,
            kafka_topic=audit_topic,
        )
    except AuditLedgerError as ex:
        logger.critical(
            "event=mutation_enqueue_audit_failed trace=%s tool=%s err=%s FAIL_CLOSED",
            trace,
            tool_name,
            ex,
        )
        return False
    try:
        await k.send_dict(ws.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
    except Exception as send_err:
        logger.critical(
            "event=mutation_kafka_send_failed trace=%s tool=%s attempt=%s err=%s",
            trace,
            tool_name,
            attempt_count,
            send_err,
        )
        try:
            await write_audit_block(
                event_type=CRAT_EVENT_MUTATION_ENQUEUE_FAILED,
                trace_id=trace,
                payload={**audit_payload, "error": str(send_err)[:500]},
                redis=r,
                kafka=k,
                kafka_topic=audit_topic,
            )
        except AuditLedgerError:
            logger.warning("event=mutation_enqueue_failed_secondary_audit_skip trace=%s", trace)
        try:
            from workers.telegram_escalation import emit_telegram_escalation

            await emit_telegram_escalation(
                ctx,
                trace,
                f"MUTATE_ENQUEUE_FAILED tool={tool_name} err={send_err!s}"[:1500],
                reason="MUTATE_ENQUEUE_FAILED",
            )
        except Exception:
            pass
        return False

    logger.info(
        "event=action_emitted action=EXECUTE_MUTATE trace=%s tool=%s attempt=%s",
        trace,
        tool_name,
        attempt_count,
    )
    fb = 0
    sdk_vr = 0
    sv_att = 0
    try:
        prev_raw = await r.get(f"omni:autonomous:state:{trace}")
        if prev_raw:
            p = json.loads(prev_raw.decode() if isinstance(prev_raw, bytes) else prev_raw)
            if isinstance(p, dict):
                fb = int(p.get("feedback_failures") or 0)
                sdk_vr = int(p.get("sdk_verify_round") or 0)
                sv_att = int(p.get("state_verify_attempt") or 0)
    except Exception:
        pass
    await r.setex(
        f"omni:autonomous:state:{trace}",
        7200,
        json.dumps(
            {
                "last_attempt_count": int(attempt_count),
                "feedback_failures": fb,
                "sdk_verify_round": sdk_vr,
                "state_verify_attempt": sv_att,
            },
            ensure_ascii=False,
        ),
    )
    return True


def _siem_hitl_required(batch: list[dict[str, Any]]) -> bool:
    """True when the originating SIEM alert explicitly requested HITL approval."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
            labels = j.get("labels") if isinstance(j, dict) else None
            if isinstance(labels, dict) and labels.get("siem_hitl_required") == "true":
                return True
        except Exception:
            continue
    return False


def _siem_alert_labels(batch: list[dict[str, Any]]) -> dict[str, str]:
    """Extract SIEM-specific labels from the canonical_query_snippet of the first matching batch item."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
            labels = j.get("labels") if isinstance(j, dict) else {}
            if isinstance(labels, dict) and labels.get("siem_source") == "finguard":
                return {k: str(v) for k, v in labels.items()}
        except Exception:
            continue
    return {}


async def emit_hitl_pending(
    ctx: Any,
    *,
    trace: str,
    tool_name: str,
    args: dict[str, Any],
    attempt_count: int = 1,
    reasoning_chain: dict[str, Any] | None = None,
    hitl_reason: str = "",
    batch: list[dict[str, Any]] | None = None,
    explain: str = "",
    advise: str = "",
) -> None:
    """
    Emit action to omni-hitl-pending instead of omni-actions — suspends execution until HITL decision.

    DEPRECATED in Advisory Mode (Phase 5): omni-hitl-pending is disabled.
    All suggestions route through Telegram + SUGGEST_REMEDIATION only.
    This function returns silently if advisory mode is active.
    """
    # Advisory Mode kill-switch: omni-hitl-pending is DISABLED if OMNI_AUTO_EXECUTE_ENABLED is false
    from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch
    from workers.advisory_hitl_compat import AdvisoryHITLCompat

    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    if ws is None:
        return

    auto_execute_enabled = bool(getattr(ws, "omni_auto_execute_enabled", False))
    siem_suggest_only = bool(getattr(ws, "omni_siem_suggest_only", True))

    if not auto_execute_enabled:
        allowed, reason = AdvisoryHITLCompat.validate_hitl_gate(trace, context="emit_hitl_pending", settings=ws)
        if not allowed:
            logger.warning("event=hitl_pending_blocked_advisory_mode trace=%s reason=%s", trace, reason)
            return
    r = getattr(ctx, "redis", None)
    if k is None or ws is None:
        return
    if r is None:
        logger.warning("event=hitl_pending_aborted trace=%s reason=missing_redis_crat_requires_redis", trace)
        return
    siem_labels = _siem_alert_labels(batch or [])
    body = build_execute_mutate_body(
        trace,
        tool_name=tool_name,
        args=args,
        attempt_count=attempt_count,
        reasoning_chain=reasoning_chain,
    )
    # Annotate with HITL metadata so dispatcher knows what to show the operator.
    body["hitl_pending"] = True
    body["hitl_reason"] = hitl_reason or "siem_critical_action"
    body["siem_incident_id"] = siem_labels.get("siem_incident_id", "")
    body["siem_tenant"] = siem_labels.get("siem_tenant", "")
    body["siem_playbook_id"] = siem_labels.get("siem_playbook_id", "")
    body["siem_category"] = siem_labels.get("siem_category", "")
    # Explain & Advise: surfaced to operators in the HITL approval UI.
    if explain:
        body["explain"] = str(explain)[:500]
    if advise:
        body["advise"] = str(advise)[:500]
    audit_topic = getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain")
    audit_payload = _mutation_enqueue_audit_payload(body)
    try:
        await write_audit_block(
            event_type=CRAT_EVENT_MUTATION_ENQUEUED,
            trace_id=trace,
            payload=audit_payload,
            redis=r,
            kafka=k,
            kafka_topic=audit_topic,
        )
    except AuditLedgerError as ex:
        logger.critical(
            "event=hitl_pending_audit_failed trace=%s tool=%s err=%s FAIL_CLOSED",
            trace,
            tool_name,
            ex,
        )
        return
    try:
        topic = getattr(ws, "kafka_topic_hitl_pending", "omni-hitl-pending")
        await k.send_dict(topic, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=hitl_pending_emitted trace=%s tool=%s siem_incident=%s",
            trace,
            tool_name,
            body["siem_incident_id"],
        )
        if r is not None:
            await r.setex(
                f"omni:hitl:state:{trace}",
                7200,
                json.dumps({"status": "PENDING_APPROVAL", "tool_name": tool_name}, ensure_ascii=False),
            )
    except Exception as e:
        logger.warning("HITL_PENDING emit skip trace=%s: %s", trace, e)


async def store_autonomous_trace_context(
    redis: Any,
    trace: str,
    *,
    batch: list[dict[str, Any]] | None = None,
    sanitized_text: str = "",
) -> None:
    """Redis snapshot for feedback-loop LLM replan and post-mutate SDK verify."""
    try:
        b = batch or []
        payload: dict[str, Any] = {
            "sanitized_text": (sanitized_text or "")[:8000],
            "batch_preview": [str(x.get("probe")) for x in b][:20],
        }
        if b:
            payload["verify_probe_ids"] = _verify_probe_ids_from_batch(b)
            payload["anomaly_event_min"] = anomaly_event_dict_from_evidence_batch(b, trace)
            payload["alertname"] = alertname_from_batch(b)
            payload["symptom_group"] = _symptom_group_from_batch(b)
            lbls = labels_dict_from_canonical_query_snippet(str(b[0].get("canonical_query_snippet") or ""))
            ev_min = payload["anomaly_event_min"]
            if isinstance(ev_min, dict):
                ovr = ev_min.get("omni_verify_required")
                if ovr is not None:
                    payload["omni_verify_required"] = ovr
                elif lbls.get("omni_verify_required") is not None:
                    payload["omni_verify_required"] = parse_omni_verify_required(lbls.get("omni_verify_required"))
                dt = str(ev_min.get("drift_type") or "").strip()
                if dt:
                    payload["drift_type"] = dt
            payload["omni.io/incident-id"] = trace
            payload["omni.io/incident-state"] = "INGESTED"
            lay = str(lbls.get("omni.io/layer") or "").strip()
            if lay:
                payload["omni.io/layer"] = lay
            sg = str(
                lbls.get("omni.io/symptom-group")
                or payload.get("symptom_group")
                or ""
            ).strip()
            if sg:
                payload["omni.io/symptom-group"] = sg
            rr = rollout_args_from_evidence_batch(b)
            if rr:
                ns_rr = str(rr.get("namespace") or "").strip()
                dep_rr = str(rr.get("deployment") or "").strip()
                if ns_rr and dep_rr:
                    payload["rollout_ns_dep"] = {"namespace": ns_rr, "deployment": dep_rr}
        await redis.setex(
            f"omni:autonomous:ctx:{trace}",
            7200,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as e:
        logger.debug("store_autonomous_trace_context: %s", e)
