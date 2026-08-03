"""Preflight enrichment: resolve Secret key refs from Deployment API before RAG/LLM.

Uses **namespace + deployment** already present in Prometheus alert labels (canonical_query_snippet),
not workload-specific hardcoding. No Secret *values* are read or logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

PROBE_PREFLIGHT = "preflight_deployment_secret_refs"


def secret_refs_from_deployment(dep: Any) -> list[dict[str, str]]:
    """Extract secretKeyRef / secretRef names from Deployment pod template (no values)."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        tpl = dep.spec.template.spec
    except Exception:
        return out
    for c in tpl.containers or []:
        for ev in c.env or []:
            vf = getattr(ev, "value_from", None)
            sk = getattr(vf, "secret_key_ref", None) if vf is not None else None
            if sk is None:
                continue
            name = str(getattr(sk, "name", "") or "").strip()
            key = str(getattr(sk, "key", "") or "").strip()
            envn = str(getattr(ev, "name", "") or "").strip()
            if not name or not key:
                continue
            tup = (name, key)
            if tup in seen:
                continue
            seen.add(tup)
            out.append(
                {
                    "secret_name": name,
                    "secret_key": key,
                    "env_var": envn,
                    "source": "env.valueFrom.secretKeyRef",
                }
            )
        for ef in c.env_from or []:
            sr = getattr(ef, "secret_ref", None)
            if sr is None:
                continue
            name = str(getattr(sr, "name", "") or "").strip()
            if not name:
                continue
            tup = (name, "envFrom")
            if tup in seen:
                continue
            seen.add(tup)
            out.append(
                {
                    "secret_name": name,
                    "secret_key": "(keys from envFrom — inspect Secret or describe Deployment)",
                    "env_var": "",
                    "source": "envFrom.secretRef",
                }
            )
    return out


async def merge_preflight_deployment_secret_refs(
    batch: list[dict[str, Any]],
    *,
    trace: str,
) -> list[dict[str, Any]]:
    """
    If evidence suggests credential failure and alert labels yield namespace+deployment,
    read Deployment once and append a synthetic probe with structured secret refs.
    """
    from pkg.reasoning.deterministic_mutate_from_evidence import _evidence_suggests_credential_failure
    from pkg.reasoning.rollout_eligibility import rollout_args_from_evidence_batch

    if any(str(b.get("probe") or "") == PROBE_PREFLIGHT for b in batch):
        return batch
    if not _evidence_suggests_credential_failure(batch):
        return batch
    rr = rollout_args_from_evidence_batch(batch)
    if not rr:
        logger.info(
            "event=preflight_deployment_secret_refs_skip trace=%s reason=no_rollout_args_from_alert_labels",
            trace,
        )
        return batch
    ns = str(rr.get("namespace") or "").strip()
    dep = str(rr.get("deployment") or "").strip()
    if not ns or not dep:
        return batch

    try:
        from kubernetes_asyncio import client

        from pkg.k8s_config import load_k8s_config as _load_k8s_config

        await _load_k8s_config()
        apps = client.AppsV1Api()
        try:
            d = await apps.read_namespaced_deployment(dep, ns)
            refs = secret_refs_from_deployment(d)
        finally:
            await apps.api_client.close()
    except Exception as e:
        logger.warning("event=preflight_deployment_secret_refs_err trace=%s err=%s", trace, e)
        return batch

    if not refs:
        logger.info(
            "event=preflight_deployment_secret_refs_empty trace=%s ns=%s deployment=%s",
            trace,
            ns,
            dep,
        )
        return batch

    block: dict[str, Any] = {
        "probe": PROBE_PREFLIGHT,
        "result": "PASSED",
        "extracted_fact": {
            "namespace": ns,
            "deployment": dep,
            "secret_refs": refs,
            "source": "kubernetes_api_read",
            "note": (
                "Resolved from Deployment pod template using namespace/deployment from Prometheus alert labels; "
                "no Secret data values were read."
            ),
        },
        "alert_hint": "Preflight: Secret name/key refs from workload Deployment (API).",
        "symptom_group": "preflight",
        "layer": "workload",
        "raw": "",
        "ts": str(int(time.time())),
    }
    logger.info(
        "event=preflight_deployment_secret_refs trace=%s ns=%s deployment=%s ref_count=%s",
        trace,
        ns,
        dep,
        len(refs),
    )
    return list(batch) + [block]
