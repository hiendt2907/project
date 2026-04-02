from __future__ import annotations

import json
import logging
import time

from workers.diagnostic_evidence import evidence_from_probe
from workers.diagnostic_mapping import classify_event, load_diagnostic_matrix
from workers.diagnostic_probe_registry import run_probe
from workers.handlers import WorkerHandlerContext
from observability.normalize import redact
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


async def run_diagnostic_pipeline(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> None:
    """Deterministic probes from YAML matrix → Redis Stream ``diagnostic:evidence``."""
    ws = ctx.settings
    if not ws.diagnostic_dictionary_enabled:
        return
    matrix = load_diagnostic_matrix(ws.diagnostic_matrix_path)
    row = classify_event(ev, matrix)
    if not row:
        logger.debug("diagnostic: no matrix row for trace=%s", ev.trace_id)
        return
    trace = ev.trace_id
    for pid in row.probe_ids:
        raw = await run_probe(pid, ctx, ev)
        ev_obj = evidence_from_probe(raw, trace)
        payload = {
            "kind": "diagnostic_evidence",
            "trace_id": trace,
            "symptom_group": row.symptom_group,
            "layer": row.layer,
            "probe": ev_obj.probe_name,
            "result": ev_obj.result,
            "extracted_fact": ev_obj.extracted_fact,
            "raw": redact(ev_obj.raw_output)[:4000],
            "ts": str(int(time.time())),
        }
        await ctx.redis.xadd(
            ws.diagnostic_evidence_stream,
            {"data": json.dumps(payload, ensure_ascii=False)},
            maxlen=ws.diagnostic_evidence_maxlen,
            approximate=True,
        )
        if row.stop_on_first_failure and raw.status == "FAILED":
            break
