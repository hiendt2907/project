"""Per-tenant discovery-doc accumulation + Mermaid diagram generation.

Dependency-light (redis client passed in, no workers/executor/prober imports) so
both the onboarding worker (Kafka-driven) and the gateway (manual handover-doc
upload + read-only diagram endpoint) share the exact same accumulation logic —
agent/plans/PLAN_onboarding_ops_agent.md step-3.

Redis namespaces:
  omni:onboarding:doc:{tenant_id}            — hash, accumulated discovery facts
  omni:onboarding:diagram:{tenant_id}:v{N}   — raw Mermaid text, versioned (append-only)
  omni:onboarding:diagram:{tenant_id}:latest — int, latest version number
  omni:onboarding:questions:{tenant_id}      — hash question_id -> JSON record
  omni:onboarding:questions_open:{tenant_id} — zset question_id -> created_at (open only)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DOC_KEY = "omni:onboarding:doc:{tenant_id}"
DIAGRAM_KEY = "omni:onboarding:diagram:{tenant_id}:v{version}"
DIAGRAM_LATEST_KEY = "omni:onboarding:diagram:{tenant_id}:latest"
QUESTIONS_KEY = "omni:onboarding:questions:{tenant_id}"
QUESTIONS_OPEN_KEY = "omni:onboarding:questions_open:{tenant_id}"

DEFAULT_READINESS_THRESHOLDS = {
    "endpoint_mapped_pct_min": 80.0,
    "business_flow_confirmed_pct_min": 80.0,
    "open_questions_max": 0,
    "open_question_stale_days": 7,
}


def _sanitize_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Data residency: handover/doc content stays on the customer's system —
    Omni keeps only a reference (hash + length), never the text itself."""
    out: list[dict[str, Any]] = []
    for doc in documents:
        content = str(doc.get("content") or "")
        out.append(
            {
                "path": doc.get("path"),
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content_length": len(content),
            }
        )
    return out


def _sanitize_services(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Data residency: tenant-authored business-purpose text stays on the
    customer's system — Omni keeps only the described/not-described mapping."""
    out: list[dict[str, Any]] = []
    for svc in services:
        description = str(svc.get("description") or "").strip()
        out.append(
            {
                "name": svc.get("name"),
                "status": svc.get("status"),
                "described": bool(description),
                "description_length": len(description),
            }
        )
    return out


def _sanitize_for_residency(probe: str, discovery_data: dict[str, Any]) -> dict[str, Any]:
    """Ánh xạ (mapping) only — strip knowledge/doc/handover content before it is
    persisted on the Omni side. Structural facts (names, ports, topology shape)
    are kept since they are mapping, not narrative knowledge."""
    if probe == "doc_snapshot" and isinstance(discovery_data.get("documents"), list):
        return {**discovery_data, "documents": _sanitize_documents(discovery_data["documents"])}
    if probe == "service_topology" and isinstance(discovery_data.get("services"), list):
        return {**discovery_data, "services": _sanitize_services(discovery_data["services"])}
    return discovery_data


async def accumulate_probe_fact(redis: Any, tenant_id: str, probe: str, discovery_data: dict[str, Any]) -> None:
    """A2/A3: fold one probe's discovery_data into the per-tenant doc hash."""
    doc_key = DOC_KEY.format(tenant_id=tenant_id)
    sanitized = _sanitize_for_residency(probe, discovery_data)
    await redis.hset(doc_key, probe, json.dumps(sanitized, ensure_ascii=False))
    await redis.hset(doc_key, f"{probe}:updated_at", str(int(time.time())))


async def get_accumulated_doc(redis: Any, tenant_id: str) -> dict[str, Any]:
    """Read back the accumulated per-probe discovery facts for a tenant."""
    raw = await redis.hgetall(DOC_KEY.format(tenant_id=tenant_id))
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k.endswith(":updated_at"):
            continue
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def render_component_diagram(doc: dict[str, Any]) -> str:
    services = (doc.get("service_topology") or {}).get("services") or []
    lines = ["graph TD"]
    if not services:
        lines.append("  host[\"host\"]")
    for svc in services[:50]:
        name = str(svc.get("name") or "svc").replace('"', "'")
        node_id = f"svc_{abs(hash(name)) % 100000}"
        lines.append(f'  {node_id}["{name}"]')
    return "\n".join(lines)


def render_api_sequence_diagram(doc: dict[str, Any]) -> str:
    ports = (doc.get("port_scan") or {}).get("listening_ports") or []
    lines = ["sequenceDiagram", "  participant Client"]
    if not ports:
        lines.append("  participant Server")
        lines.append("  Client->>Server: (no listening ports discovered yet)")
        return "\n".join(lines)
    for p in ports[:20]:
        port = p.get("port")
        service = str(p.get("service") or f"port-{port}").replace('"', "'")
        participant = f"Server_{port}"
        lines.append(f"  participant {participant} as {service} ({port})")
    for p in ports[:20]:
        port = p.get("port")
        service = str(p.get("service") or f"port-{port}")
        lines.append(f"  Client->>Server_{port}: request ({service})")
    return "\n".join(lines)


def render_business_flow_diagram(doc: dict[str, Any]) -> str:
    processes = (doc.get("process_list") or {}).get("processes") or []
    lines = ["flowchart LR", "  start([\"Request\"])"]
    prev = "start"
    for proc in processes[:15]:
        name = str(proc.get("name") or "proc").replace('"', "'")
        node_id = f"p_{abs(hash(name)) % 100000}"
        lines.append(f'  {node_id}["{name}"]')
        lines.append(f"  {prev} --> {node_id}")
        prev = node_id
    lines.append(f'  {prev} --> finish(["Response"])')
    return "\n".join(lines)


def render_all_diagrams(doc: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            "%% component architecture",
            render_component_diagram(doc),
            "%% API sequence",
            render_api_sequence_diagram(doc),
            "%% business flow",
            render_business_flow_diagram(doc),
        ]
    )


async def regenerate_diagrams(redis: Any, tenant_id: str) -> int:
    """A3: render 3 Mermaid diagram types from the accumulated doc, save as a new
    immutable version (never overwrite — diffable history). Returns the new version."""
    doc = await get_accumulated_doc(redis, tenant_id)
    rendered = render_all_diagrams(doc)
    latest_key = DIAGRAM_LATEST_KEY.format(tenant_id=tenant_id)
    version = await redis.incr(latest_key)
    await redis.set(DIAGRAM_KEY.format(tenant_id=tenant_id, version=version), rendered)
    return int(version)


async def get_latest_diagram(redis: Any, tenant_id: str) -> tuple[int, str] | None:
    latest_key = DIAGRAM_LATEST_KEY.format(tenant_id=tenant_id)
    raw_version = await redis.get(latest_key)
    if not raw_version:
        return None
    version = int(raw_version)
    text = await redis.get(DIAGRAM_KEY.format(tenant_id=tenant_id, version=version))
    if text is None:
        return None
    return version, text


async def get_diagram_version(redis: Any, tenant_id: str, version: int) -> str | None:
    return await redis.get(DIAGRAM_KEY.format(tenant_id=tenant_id, version=version))


def compute_mapped_pct(doc: dict[str, Any]) -> float:
    ports = (doc.get("port_scan") or {}).get("listening_ports") or []
    if not ports:
        return 0.0
    mapped = [p for p in ports if str(p.get("service") or "").strip()]
    return round(100.0 * len(mapped) / len(ports), 2)


def compute_business_flow_pct(doc: dict[str, Any]) -> float:
    services = (doc.get("service_topology") or {}).get("services") or []
    if not services:
        return 0.0
    described = [s for s in services if s.get("described")]
    return round(100.0 * len(described) / len(services), 2)


async def count_stale_open_questions(redis: Any, tenant_id: str, *, stale_days: float) -> int:
    """Open questions older than ``stale_days`` — dynamic count, not a static counter."""
    cutoff = time.time() - stale_days * 86400
    return int(await redis.zcount(QUESTIONS_OPEN_KEY.format(tenant_id=tenant_id), "-inf", cutoff) or 0)


async def resolve_readiness_thresholds(admin_repo: Any, tenant_id: str) -> dict[str, float]:
    """Thresholds read from omni_admin.runtime_flag (per-tenant override, else global
    default key); never hardcoded beyond DEFAULT_READINESS_THRESHOLDS as the fallback."""
    if admin_repo is None:
        return dict(DEFAULT_READINESS_THRESHOLDS)
    for flag_key in (f"readiness_threshold:{tenant_id}", "readiness_threshold:default"):
        try:
            value = await admin_repo.get_runtime_flag(flag_key, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("discovery_doc: threshold lookup failed key=%s err=%s", flag_key, exc)
            value = None
        if isinstance(value, dict):
            merged = dict(DEFAULT_READINESS_THRESHOLDS)
            merged.update(value)
            return merged
    return dict(DEFAULT_READINESS_THRESHOLDS)


async def compute_readiness(redis: Any, admin_repo: Any, tenant_id: str) -> dict[str, Any]:
    """A6/A7: recompute the readiness checklist from the accumulated doc + thresholds.

    Returns the raw fields (caller persists via AdminConfigRepo.set_tenant_readiness) —
    kept side-effect-free here so gateway and worker share identical math.
    """
    doc = await get_accumulated_doc(redis, tenant_id)
    thresholds = await resolve_readiness_thresholds(admin_repo, tenant_id)
    endpoint_mapped_pct = compute_mapped_pct(doc)
    business_flow_confirmed_pct = compute_business_flow_pct(doc)
    stale_open = await count_stale_open_questions(
        redis, tenant_id, stale_days=float(thresholds["open_question_stale_days"]),
    )
    ready = (
        endpoint_mapped_pct >= float(thresholds["endpoint_mapped_pct_min"])
        and business_flow_confirmed_pct >= float(thresholds["business_flow_confirmed_pct_min"])
        and stale_open <= int(thresholds["open_questions_max"])
    )
    return {
        "endpoint_mapped_pct": endpoint_mapped_pct,
        "business_flow_confirmed_pct": business_flow_confirmed_pct,
        "open_questions_over_threshold": stale_open,
        "readiness_flag": ready,
    }
