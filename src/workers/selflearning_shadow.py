"""Shadow self-learning helpers (non-impact by default).

All behaviors here are opt-in and must not alter runtime decision flow when disabled.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _flag(settings: Any, name: str, default: bool = False) -> bool:
    return bool(getattr(settings, name, default))


def _derive_probe_suggestions(text: str) -> list[str]:
    t = (text or "").lower()
    probes: list[str] = []
    if "dns" in t or "ndots" in t:
        probes.extend(["inspect_dns_config", "probe_dns_resolution"])
    if "latency" in t or "timeout" in t:
        probes.extend(["inspect_service_latency", "probe_network_path"])
    if "oom" in t or "memory" in t:
        probes.extend(["inspect_memory_breakdown", "probe_memory_trend"])
    if "cpu" in t or "throttl" in t:
        probes.extend(["probe_cpu_throttling", "verify_sigma_snapshot"])
    if "kafka" in t or "lag" in t:
        probes.extend(["inspect_kafka_lag", "inspect_partition_skew"])
    if "redis" in t:
        probes.extend(["inspect_redis_memory", "inspect_redis_backlog"])
    out: list[str] = []
    for p in probes:
        if p not in out:
            out.append(p)
    return out[:8]


async def _generate_three_hypotheses(
    ctx: Any,
    *,
    trace: str,
    sanitized_text: str,
) -> list[dict[str, Any]]:
    ollama = getattr(ctx, "ollama", None)
    ws = getattr(ctx, "settings", None)
    if ollama is None or ws is None:
        return []
    model = (
        str(getattr(ws, "diag_evidence_llm_model", "") or "").strip()
        or str(getattr(ws, "model_reasoning_engine", "") or "").strip()
        or str(getattr(ws, "chat_model", "") or "").strip()
    )
    if not model:
        return []
    system = (
        "Return exactly one JSON object: "
        '{"hypotheses":[{"name":"...","why":"...","confidence":0.0-1.0}]} '
        "with 3 concise hypotheses grounded in evidence."
    )
    user = f"trace={trace}\nEvidence:\n{sanitized_text[:8000]}"
    try:
        resp = await ollama.chat(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            stream=False,
        )
        msg = (resp or {}).get("message") or {}
        content = str(msg.get("content") or "").strip()
        i, j = content.find("{"), content.rfind("}")
        if i < 0 or j <= i:
            return []
        payload = json.loads(content[i : j + 1])
        rows = payload.get("hypotheses") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "name": str(row.get("name") or "").strip()[:120],
                    "why": str(row.get("why") or "").strip()[:500],
                    "confidence": float(row.get("confidence") or 0.0),
                }
            )
        return [x for x in out if x.get("name")]
    except Exception as e:
        logger.debug("[%s] shadow hypotheses skip: %s", trace, e)
        return []


async def run_shadow_selflearning(
    ctx: Any,
    *,
    trace: str,
    sanitized_text: str,
    machine: dict[str, Any] | None = None,
) -> None:
    ws = getattr(ctx, "settings", None)
    redis = getattr(ctx, "redis", None)
    if ws is None or redis is None:
        return
    enabled = _flag(ws, "multi_hypothesis_enabled", False) or _flag(ws, "knowledge_draft_enabled", False)
    if not enabled:
        return

    hyp: list[dict[str, Any]] = []
    if _flag(ws, "multi_hypothesis_enabled", False):
        hyp = await _generate_three_hypotheses(ctx, trace=trace, sanitized_text=sanitized_text)
    if not hyp and machine:
        hyp = [{"name": str(machine.get("hypothesis") or "single_hypothesis"), "why": "from machine json", "confidence": 0.5}]

    probes = _derive_probe_suggestions(sanitized_text) if _flag(ws, "deep_probe_orchestration_enabled", False) else []
    draft = {
        "trace_id": trace,
        "ts": int(time.time()),
        "shadow_only": _flag(ws, "multi_hypothesis_shadow_only", True),
        "hypotheses": hyp,
        "probe_suggestions": probes,
        "knowledge_draft": {
            "symptom": str(sanitized_text or "")[:800],
            "root_cause": (hyp[0]["name"] if hyp else ""),
            "fix": "requires_human_review_before_promotion",
        }
        if _flag(ws, "knowledge_draft_enabled", False)
        else {},
    }
    key = f"omni:selflearn:shadow:{trace}"
    try:
        await redis.setex(key, 86400, json.dumps(draft, ensure_ascii=False))
        logger.info(
            "event=selflearning_shadow trace=%s hypotheses=%s probes=%s promotion=%s git_push=%s",
            trace,
            len(hyp),
            len(probes),
            _flag(ws, "knowledge_promotion_enabled", False),
            _flag(ws, "autodoc_git_push_enabled", False),
        )
    except Exception as e:
        logger.debug("[%s] selflearning shadow store skip: %s", trace, e)
