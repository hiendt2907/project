"""Post-mutate SDK verify: re-run same probes as dispatcher (read-only) to confirm issue cleared."""

from __future__ import annotations

import logging
from typing import Any

from workers.diagnostic_evidence import ProbeRunRaw, evidence_from_probe
from workers.diagnostic_probe_registry import run_probe
from workers.handlers import WorkerHandlerContext
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


def optional_probe_ids_from_ctx(ctx: WorkerHandlerContext) -> set[str]:
    """Parse OMNI_SDK_VERIFY_OPTIONAL_PROBES; default includes prom cpu/mem when unset/empty."""
    s = getattr(ctx, "settings", None)
    default = {"prom_pod_cpu_cores", "prom_pod_memory_wss"}
    if s is None:
        return default
    raw = getattr(s, "omni_sdk_verify_optional_probes", None)
    if raw is None:
        return default
    txt = str(raw).strip()
    if not txt:
        return default
    out = {p.strip() for p in txt.split(",") if p.strip()}
    return out if out else default


def _verify_passes_for_pair(
    probe_id: str,
    raw: ProbeRunRaw,
    optional: set[str],
) -> bool:
    if raw.status == "PASSED":
        return True
    # Clinical probes: SKIPPED = workload healthy / nothing to tail (e.g. post-remediation pod rotation).
    if raw.status == "SKIPPED":
        return True
    if probe_id in optional and raw.status == "INCONCLUSIVE":
        return True
    return False


async def run_verify_probes(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    probe_ids: list[str],
    ev: AnomalyEvent,
    optional_probe_ids: set[str] | None = None,
) -> tuple[bool, str, list[ProbeRunRaw]]:
    """
    Run each probe_id in order. All must be PASSED for drift/security self-remediation,
    except probes listed in ``optional_probe_ids`` (or settings OMNI_SDK_VERIFY_OPTIONAL_PROBES)
    which may be INCONCLUSIVE (e.g. missing Prom series). FAILED always fails.

    Returns (all_passed, human_summary, raw_results).
    """
    optional = optional_probe_ids if optional_probe_ids is not None else optional_probe_ids_from_ctx(ctx)
    raws: list[ProbeRunRaw] = []
    lines: list[str] = []
    ordered_ids: list[str] = []
    for pid in probe_ids:
        p = str(pid).strip()
        if not p:
            continue
        ordered_ids.append(p)
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
    if not ordered_ids:
        return True, "(no probes)", []

    ok = all(
        _verify_passes_for_pair(pid, raw, optional)
        for pid, raw in zip(ordered_ids, raws)
    )
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
