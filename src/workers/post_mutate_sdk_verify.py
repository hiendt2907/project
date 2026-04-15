"""Post-mutate SDK verify: re-run same probes as dispatcher (read-only) to confirm issue cleared."""

from __future__ import annotations

import logging
from typing import Any

from workers.diagnostic_evidence import ProbeRunRaw, evidence_from_probe
from workers.diagnostic_probe_registry import run_probe
from workers.handlers import WorkerHandlerContext
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


async def run_verify_probes(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    probe_ids: list[str],
    ev: AnomalyEvent,
) -> tuple[bool, str, list[ProbeRunRaw]]:
    """
    Run each probe_id in order. All must be PASSED for drift/security self-remediation.

    Returns (all_passed, human_summary, raw_results).
    """
    raws: list[ProbeRunRaw] = []
    lines: list[str] = []
    for pid in probe_ids:
        p = str(pid).strip()
        if not p:
            continue
        try:
            raw = await run_probe(p, ctx, ev)
        except Exception as e:
            logger.warning("[%s] verify probe %s error: %s", trace, p, e)
            raw = ProbeRunRaw(
                probe_name=p,
                status="INCONCLUSIVE",
                raw_text=str(e)[:2000],
                structured_hint={"error": str(e)[:500]},
            )
        raws.append(raw)
        lines.append(f"{p}={raw.status} {raw.raw_text[:200]}")
    if not probe_ids:
        return True, "(no probes)", []

    ok = all(r.status == "PASSED" for r in raws)
    summary = "\n".join(lines)[:4000]
    return ok, summary, raws


def probe_raws_to_batch_for_deterministic(trace: str, raws: list[ProbeRunRaw], *, symptom_group: str) -> list[dict[str, Any]]:
    """Build evidence-shaped batch items for ``deterministic_mutate_plan_from_batch``."""
    out: list[dict[str, Any]] = []
    for raw in raws:
        eo = evidence_from_probe(raw, trace)
        out.append(
            {
                "probe": eo.probe_name,
                "result": eo.result,
                "extracted_fact": eo.extracted_fact,
                "symptom_group": symptom_group or "",
                "alert_hint": "",
            }
        )
    return out
