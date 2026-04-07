"""Helpers to emit EXECUTE_MUTATE from evidence batch (namespace/deployment from alert labels)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pkg.autonomous_actions import build_execute_mutate_body

logger = logging.getLogger(__name__)

# Workload CPU / resource alerts — rollout gate sau RAG/diagnosis.
_RE_CPU_INCIDENT = re.compile(
    r"(highcpu|\bcpu\b|cpu\s+utilization|millicore|millicores|cgroup)",
    re.IGNORECASE,
)
_RE_FAULT_INCIDENT = re.compile(
    r"(createcontainer|crashloop|imagepull|probefail|readiness|liveness|backoff|oom|oomkilled|failedmount|unschedul)",
    re.IGNORECASE,
)


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
            dep = str(labels.get("deployment") or "").strip()
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
) -> None:
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    r = getattr(ctx, "redis", None)
    if k is None or ws is None:
        return
    body = build_execute_mutate_body(
        trace,
        tool_name=tool_name,
        args=args,
        attempt_count=attempt_count,
    )
    try:
        await k.send_dict(ws.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_emitted action=EXECUTE_MUTATE trace=%s tool=%s attempt=%s",
            trace,
            tool_name,
            attempt_count,
        )
        if r is not None:
            fb = 0
            try:
                prev_raw = await r.get(f"omni:autonomous:state:{trace}")
                if prev_raw:
                    p = json.loads(prev_raw.decode() if isinstance(prev_raw, bytes) else prev_raw)
                    if isinstance(p, dict):
                        fb = int(p.get("feedback_failures") or 0)
            except Exception:
                pass
            await r.setex(
                f"omni:autonomous:state:{trace}",
                7200,
                json.dumps(
                    {
                        "last_attempt_count": int(attempt_count),
                        "feedback_failures": fb,
                    },
                    ensure_ascii=False,
                ),
            )
    except Exception as e:
        logger.warning("EXECUTE_MUTATE emit skip: %s", e)


async def store_autonomous_trace_context(
    redis: Any,
    trace: str,
    *,
    batch: list[dict[str, Any]] | None = None,
    sanitized_text: str = "",
) -> None:
    """Redis snapshot for feedback-loop LLM replan."""
    try:
        payload = {
            "sanitized_text": (sanitized_text or "")[:8000],
            "batch_preview": [str(x.get("probe")) for x in (batch or [])][:20],
        }
        await redis.setex(
            f"omni:autonomous:ctx:{trace}",
            7200,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as e:
        logger.debug("store_autonomous_trace_context: %s", e)
