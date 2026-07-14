"""Onboarding routes — read-only Mermaid diagram + manual handover-doc upload.

step-3 của agent/plans/PLAN_onboarding_ops_agent.md. Uses pkg.onboarding.discovery_doc
(dependency-light, shared with the onboarding worker) — gateway never imports workers/.
"""
from __future__ import annotations

import json
import logging
import hashlib
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.tenant_context import get_tenant_ctx, resolve_scope
from pkg.onboarding import discovery_doc as dd

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

_TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


def _get_admin_repo(request: Request) -> Any:
    repo = getattr(request.app.state, "admin_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail="Admin config store offline (OMNI_ADMIN_PG_DSN chưa cấu hình)",
        )
    return repo


def _effective_tenant_id(request: Request, tenant_id: str | None) -> str:
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, tenant_id)
    return scope or tenant_id or "default"


@router.get("/diagram")
async def get_diagram(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Latest versioned Mermaid diagram text (raw — never rendered to image)."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    result = await dd.get_latest_diagram(redis, scope)
    if result is None:
        return JSONResponse(content={"tenant_id": scope, "version": None, "mermaid": None})
    version, text = result
    return JSONResponse(content={"tenant_id": scope, "version": version, "mermaid": text})


# Versions are append-only (INCR, no TTL) so gaps are rare; the probe cap only
# guards against a pathological store while keeping the scan bounded.
_HISTORY_MAX_PROBES = 200


@router.get("/diagram/history")
async def get_diagram_history(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
    before: int | None = Query(default=None, ge=2),
    limit: int = Query(default=10, ge=1, le=50),
) -> JSONResponse:
    """Past diagram versions (diffable), newest-first, anchored at the latest
    version and walking DOWN. `before` paginates older pages (versions strictly
    below it); `next_before` in the response feeds the next page request."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    raw_latest = await redis.get(dd.DIAGRAM_LATEST_KEY.format(tenant_id=scope))
    latest = int(raw_latest) if raw_latest else None
    if latest is None:
        return JSONResponse(content={"tenant_id": scope, "latest": None, "versions": [], "next_before": None})

    start = min(before - 1, latest) if before is not None else latest
    versions: list[dict[str, Any]] = []
    v = start
    probes = 0
    while v >= 1 and len(versions) < limit and probes < _HISTORY_MAX_PROBES:
        text = await dd.get_diagram_version(redis, scope, v)
        if text is not None:
            versions.append({"version": v, "mermaid": text})
        v -= 1
        probes += 1
    next_before = versions[-1]["version"] if versions and versions[-1]["version"] > 1 else None
    return JSONResponse(content={"tenant_id": scope, "latest": latest, "versions": versions, "next_before": next_before})


@router.get("/doc")
async def get_doc(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Accumulated raw discovery facts per probe — for the onboarding UI."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    doc = await dd.get_accumulated_doc(redis, scope)
    return JSONResponse(content={"tenant_id": scope, "doc": doc})


@router.get("/readiness")
async def get_readiness(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Readiness checklist (Postgres source-of-truth via AdminConfigRepo).

    Trả kèm `thresholds` để portal hiển thị "đạt X% so với mục tiêu Y%" (ADR-003 —
    operator không cần biết config nội bộ mới đọc được checklist)."""
    repo = _get_admin_repo(request)
    scope = _effective_tenant_id(request, tenant_id)
    readiness = await repo.get_tenant_readiness(scope)
    thresholds = await dd.resolve_readiness_thresholds(repo, scope)
    return JSONResponse(content={"tenant_id": scope, "readiness": readiness, "thresholds": thresholds})


@router.get("/adapters")
async def get_domain_adapters() -> JSONResponse:
    """Domain-neutral capability catalog for provider/operator surfaces.

    Kubernetes is deliberately returned beside Linux, database and network
    adapters.  The endpoint is metadata-only: it does not authorize or execute
    commands; the durable command runtime still enforces tenant, approval and
    verification gates.
    """
    from aoip.domain_adapters import default_registry

    adapters = []
    for descriptor in default_registry().list_adapters():
        adapters.append({
            "name": descriptor.name,
            "domain": descriptor.domain,
            "version": descriptor.version,
            "capabilities": [
                {
                    "name": capability.name,
                    "operations": list(capability.operations),
                    "mutating": capability.mutating,
                    "requires_approval": capability.requires_approval,
                    "verification_required": capability.verification_required,
                }
                for capability in descriptor.capabilities
            ],
        })
    return JSONResponse(content={"adapters": adapters})


class HandoverDocUpload(BaseModel):
    filename: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=8000)
    tenant_id: str | None = Field(default=None, pattern=_TENANT_ID_PATTERN)


@router.post("/handover-doc")
async def upload_handover_doc(request: Request, body: HandoverDocUpload) -> JSONResponse:
    """A8: manually-uploaded handover doc — feeds the same A3 accumulation pipeline
    as a doc_snapshot probe, without going through the remote agent/Kafka.

    Data residency: content is hashed in dd.accumulate_probe_fact's sanitization
    step and never persisted on the Omni side — only path/hash/length."""
    redis = _get_redis(request)
    scope = _effective_tenant_id(request, body.tenant_id)
    contract = _parse_uploaded_api_contract(body.content, body.filename)
    discovery_data = {"documents": [{"path": body.filename, "content": body.content}]}
    try:
        await dd.accumulate_probe_fact(redis, scope, "doc_snapshot", discovery_data)
        if contract:
            await dd.accumulate_probe_fact(redis, scope, "api_contract", {"api_contracts": [contract]})
        version = await dd.regenerate_diagrams(redis, scope)
    except Exception as exc:
        logger.error("onboarding.upload_handover_doc error tenant=%s err=%s", scope, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    repo = getattr(request.app.state, "admin_repo", None)
    readiness: dict[str, Any] | None = None
    if repo is not None:
        try:
            fields = await dd.compute_readiness(redis, repo, scope)
            readiness = await repo.set_tenant_readiness(tenant_id=scope, **fields)
        except Exception as exc:  # noqa: BLE001 — readiness recompute best-effort here
            logger.warning("onboarding.upload_handover_doc readiness recompute failed tenant=%s err=%s", scope, exc)

    return JSONResponse(content={"status": "ok", "tenant_id": scope, "diagram_version": version, "readiness": readiness})


# ── Slice O2A/O2B — Competency Matrix + Unknown/Question read API ──────────
# aoip has no dependency on workers/executor (same import-boundary rule as
# pkg.onboarding above) — safe to import directly from the gateway.

_ENTITY_TYPE_PATTERN = r"^(host|service)$"


@router.get("/competency")
async def get_entity_competency(
    request: Request,
    entity_type: str = Query(..., pattern=_ENTITY_TYPE_PATTERN),
    entity_id: str = Query(..., min_length=1, max_length=200),
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Entity Competency Matrix (Slice O2A) — facet/state/evidence/contradiction
    for one Host or Service, derived on demand from the persisted SystemModel."""
    from aoip.competency_matrix import build_entity_competency_from_store, contradicted_facets, critical_unknowns, entity_coverage

    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    comp = await build_entity_competency_from_store(redis, scope, entity_type=entity_type, entity_id=entity_id)
    return JSONResponse(content={
        "tenant_id": scope,
        "entity_type": comp.entity_type,
        "entity_id": comp.entity_id,
        "facets": {name: {
            "state": fv.state.value,
            "value": fv.value,
            "evidence_refs": list(fv.evidence_refs),
            "source_types": list(fv.source_types),
            "confidence": fv.confidence,
            "last_observed_at": fv.last_observed_at,
            "last_verified_at": fv.last_verified_at,
        } for name, fv in comp.facets.items()},
        "coverage": entity_coverage(comp),
        "critical_unknowns": list(critical_unknowns(comp)),
        "contradicted_facets": list(contradicted_facets(comp)),
    })


@router.get("/entities")
async def get_entities(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Entity index của System Twin — mọi host/service đã biết, kèm revision.

    Điểm vào cho operator surface (Phase-2 Golden Journey Read-only): UI dùng
    danh sách này để trỏ tiếp vào ``/onboarding/competency`` per entity, thay vì
    bắt operator tự đoán ``entity_id`` (Known Broken Link #4 trong PRODUCT_PROOF)."""
    from aoip.system_model_store import load_system_model

    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    model, revision = await load_system_model(redis, scope)
    nodes = model.known_nodes
    return JSONResponse(content={
        "tenant_id": scope,
        "revision": revision,
        "hosts": sorted(n for n in nodes if n.startswith("host:")),
        "services": sorted(n for n in nodes if n.startswith("svc:")),
    })


@router.get("/system-twin")
async def get_system_twin(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Single read model for the operator's current understanding of a tenant.

    This is intentionally a projection over the canonical SystemModel store,
    not a second source of truth.  It exposes revision, graph edges, known
    entities, unresolved edge targets, contradictions, and competency unknowns
    in one tenant-scoped response so callers do not have to reconstruct the
    meaning of the twin from several endpoints.
    """
    from aoip.question_lifecycle import list_unknowns
    from aoip.system_model_store import load_contradictions, load_system_model

    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    model, revision = await load_system_model(redis, scope)
    contradictions = await load_contradictions(redis, scope)
    unknowns = await list_unknowns(redis, scope)
    hosts = sorted(n for n in model.known_nodes if n.startswith("host:"))
    services = sorted(n for n in model.known_nodes if n.startswith("svc:"))
    raw_doc = await redis.hgetall(dd.DOC_KEY.format(tenant_id=scope))
    operational_hosts = _build_operational_hosts(model.facts, hosts, raw_doc)
    api_sequence = _build_api_sequence(raw_doc)
    edges = [
        {"subject": fact.subject, "predicate": fact.predicate, "object": fact.obj,
         "confidence": fact.confidence, "provenance": list(fact.provenance),
         "verified_time": fact.verified_time}
        for fact in model.edges
    ]
    unknown_edge_targets = sorted(model.unknown_edge_targets)
    return JSONResponse(content={
        "tenant_id": scope,
        "revision": revision,
        "summary": {
            "hosts": len(hosts),
            "services": len(services),
            "edges": len(edges),
            "unknown_edge_targets": unknown_edge_targets,
            "contradictions": len(contradictions),
            "unknowns": len(unknowns),
        },
        "entities": {"hosts": hosts, "services": services},
        "operational_hosts": operational_hosts,
        "api_sequence": api_sequence,
        "edges": edges,
        "unknown_edge_targets": unknown_edge_targets,
        "contradictions": contradictions,
        "unknowns": unknowns,
    })


def _build_operational_hosts(
    facts: tuple[Any, ...], hosts: list[str], raw_doc: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build the operator-facing host/service/port projection from observed facts.

    This deliberately keeps platform daemons in the raw facts stream but gives the
    UI a compact architecture view. Port ownership comes from the per-host raw
    port_scan snapshot; facts without that snapshot remain unclassified. No
    service dependency is invented from a process name alone.
    """
    port_map: dict[str, dict[str, set[str]]] = {}
    for key, raw in (raw_doc or {}).items():
        if key.endswith(":updated_at"):
            continue
        probe, separator, hostname = key.partition("@")
        if separator != "@" or probe != "port_scan":
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        by_service = port_map.setdefault(hostname, {})
        if not isinstance(payload, dict):
            continue
        for port in payload.get("listening_ports") or []:
            if not isinstance(port, dict):
                continue
            number = port.get("port")
            if number is None:
                continue
            service = str(port.get("service") or "unclassified")
            by_service.setdefault(service, set()).add(str(number))

    result: list[dict[str, Any]] = []
    for host in hosts:
        host_facts = [fact for fact in facts if fact.subject == host]
        service_names: set[str] = set()
        for fact in host_facts:
            if fact.predicate == "runs_service":
                service_names.add(str(fact.obj))
            elif fact.predicate == "hosts" and str(fact.obj).startswith("svc:"):
                service_names.add(str(fact.obj).split(":", 1)[1])

        services: list[dict[str, Any]] = []
        for name in sorted(service_names):
            service_facts = [
                fact for fact in host_facts
                if (fact.predicate == "runs_service" and str(fact.obj) == name)
                or (fact.predicate == "hosts" and str(fact.obj) == f"svc:{name}")
            ]
            host_name = host.split(":", 1)[-1]
            ports = sorted(port_map.get(host_name, {}).get(name, set()), key=lambda value: int(value) if value.isdigit() else value)
            services.append({
                "name": name,
                "ports": ports,
                "confidence": max((fact.confidence for fact in service_facts), default=0.0),
                "provenance": list(next(iter(service_facts)).provenance) if service_facts else [],
            })

        connections = [
            {"target": str(fact.obj), "confidence": fact.confidence, "provenance": list(fact.provenance)}
            for fact in host_facts if fact.predicate == "connects_to"
        ]
        host_name = host.split(":", 1)[-1]
        observed_ports = sorted({
            port for service_ports in port_map.get(host_name, {}).values() for port in service_ports
        }, key=lambda value: int(value) if value.isdigit() else value)
        result.append({"host": host, "ports": observed_ports, "services": services, "connections": connections})
    return result


def _build_api_sequence(raw_doc: dict[str, str] | None) -> dict[str, Any]:
    """Project redacted access-log metadata without inventing downstream hops.

    ``source_host`` is the agent host that owns the access log. ``target_host``
    is present only when the log itself emitted an upstream/backend field. A
    connection scan can explain network dependency, but cannot promote it to an
    HTTP sequence.
    """
    contracts: list[dict[str, Any]] = []
    access: list[dict[str, Any]] = []
    for key, raw in (raw_doc or {}).items():
        if key.endswith(":updated_at"):
            continue
        probe, separator, hostname = key.partition("@")
        if separator != "@" or probe not in {"api_access", "api_contract"}:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if probe == "api_contract":
            for contract in payload.get("api_contracts") or []:
                if isinstance(contract, dict):
                    contracts.append({**contract, "source_host": f"host:{hostname}"})
            continue
        for item in payload.get("api_interactions") or []:
            if not isinstance(item, dict) or not item.get("route") or not item.get("method"):
                continue
            upstream = str(item.get("upstream") or "")[:120]
            access.append({
                "source_host": f"host:{hostname}",
                "target_host": upstream or None,
                "method": str(item["method"]).upper(),
                "route": str(item["route"])[:160],
                "status_class": str(item.get("status_class") or "unknown"),
                "count": int(item.get("count") or 1),
                "source_path": str(item.get("source_path") or "")[:200],
                "confidence": 0.9 if upstream else 0.8,
                "provenance": f"discovery:api_access:{hostname}",
            })
    if contracts:
        interactions: list[dict[str, Any]] = []
        for contract in contracts:
            for route in contract.get("routes") or []:
                if not isinstance(route, dict):
                    continue
                matches = [item for item in access if item["source_host"] == contract["source_host"] and item["method"] == route.get("method") and _api_route_shape(item["route"]) == _api_route_shape(route.get("route"), contract.get("base_path"))]
                count = sum(int(item.get("count") or 0) for item in matches)
                statuses = sorted({str(item.get("status_class") or "unknown") for item in matches})
                interactions.append({
                    "source_host": contract["source_host"],
                    "target_host": next((item.get("target_host") for item in matches if item.get("target_host")), None),
                    "method": str(route.get("method") or "").upper(),
                    "route": str(route.get("route") or "")[:160],
                    "operation_id": str(route.get("operation_id") or "")[:120],
                    "status_class": ", ".join(statuses) if statuses else "contract only",
                    "count": count,
                    "runtime_observed": bool(matches),
                    "confidence": 0.95 if matches else 0.8,
                    "provenance": f"api_contract:{contract.get('path', '')}",
                })
        interactions.sort(key=lambda item: (item["source_host"], item["route"], item["method"]))
        runtime_verified = any(item["runtime_observed"] for item in interactions)
        return {
            "status": "runtime_verified" if runtime_verified else "contract_observed",
            "evidence": "openapi_swagger_contract" if not runtime_verified else "openapi_swagger_plus_access_log",
            "interactions": interactions[:200],
            "unknown_reasons": [] if runtime_verified else ["Contract found; runtime access-log correlation is not observed yet."],
        }
    if access:
        return {
            "status": "missing_contract",
            "evidence": "access_log_metadata",
            "interactions": [],
            "unknown_reasons": ["Access-log routes exist, but an OpenAPI/Swagger contract is required before drawing API sequence."],
        }
    return {
        "status": "network_only",
        "evidence": "connection_scan",
        "interactions": [],
        "unknown_reasons": ["No redacted access-log route/method metadata observed yet."],
    }


def _parse_uploaded_api_contract(content: str, filename: str) -> dict[str, Any] | None:
    """Parse a supplied OpenAPI/Swagger document, retaining metadata only."""
    try:
        if filename.lower().endswith(".json"):
            document = json.loads(content)
        else:
            import yaml
            document = yaml.safe_load(content)
    except Exception:
        return None
    if not isinstance(document, dict) or not (document.get("openapi") or document.get("swagger")):
        return None
    routes: list[dict[str, Any]] = []
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    for route, item in (document.get("paths") or {}).items():
        if not isinstance(route, str) or not route.startswith("/") or not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in methods or not isinstance(operation, dict):
                continue
            routes.append({"method": method.upper(), "route": route[:200], "operation_id": str(operation.get("operationId") or "")[:120], "tags": [str(x)[:80] for x in (operation.get("tags") or [])[:10]], "response_statuses": [str(x)[:20] for x in ((operation.get("responses") or {}).keys())][:20]})
            if len(routes) >= 500:
                break
    if not routes:
        return None
    base_path = str(document.get("basePath") or "")[:120]
    if not base_path and document.get("servers") and isinstance(document["servers"][0], dict):
        from urllib.parse import urlparse
        base_path = urlparse(str(document["servers"][0].get("url") or "")).path[:120]
    return {"path": filename[:200], "format": "openapi" if document.get("openapi") else "swagger", "version": str(document.get("openapi") or document.get("swagger"))[:30], "title": str((document.get("info") or {}).get("title") or "")[:160], "base_path": base_path, "routes": routes, "content_hash": hashlib.sha256(content.encode()).hexdigest()}


def _api_route_shape(route: Any, base_path: Any = "") -> str:
    """Canonicalize OpenAPI `{id}` and access-log `:id` route shapes."""
    path = str(route or "").split("?", 1)[0].rstrip("/") or "/"
    prefix = str(base_path or "").rstrip("/")
    if prefix and not (path == prefix or path.startswith(f"{prefix}/")):
        path = f"{prefix}/{path.lstrip('/')}"
    path = re.sub(r"\{[^}/]+\}", ":id", path)
    return path.rstrip("/") or "/"


@router.get("/unknowns")
async def get_unknowns(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Structured Unknowns (Slice O2B) — what Omni still doesn't know per entity/facet."""
    from aoip.question_lifecycle import list_unknowns

    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    unknowns = await list_unknowns(redis, scope)
    return JSONResponse(content={"tenant_id": scope, "unknowns": unknowns})


@router.get("/questions")
async def get_questions(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Structured Questions (Slice O2B) — pending/answered/resolved, per entity/facet."""
    from aoip.question_lifecycle import list_questions

    redis = _get_redis(request)
    scope = _effective_tenant_id(request, tenant_id)
    questions = await list_questions(redis, scope)
    return JSONResponse(content={"tenant_id": scope, "questions": questions})


class AnswerQuestionBody(BaseModel):
    answered_by: str = Field(..., min_length=1, max_length=120)
    value: str = Field(..., min_length=1, max_length=500)
    source_channel: str = Field(default="api")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    tenant_id: str | None = Field(default=None, pattern=_TENANT_ID_PATTERN)


@router.post("/questions/{question_id}/answer")
async def answer_question(request: Request, question_id: str, body: AnswerQuestionBody) -> JSONResponse:
    """Human answer -> Claim (Slice O2B). Never marked VERIFIED here — only
    ``competency_matrix`` promotes a Claim to VERIFIED, by cross-checking a
    matching machine Fact."""
    from aoip.question_lifecycle import submit_answer

    redis = _get_redis(request)
    scope = _effective_tenant_id(request, body.tenant_id)
    answer = await submit_answer(
        redis, scope, question_id,
        answered_by=body.answered_by, value=body.value,
        source_channel=body.source_channel, confidence=body.confidence,
    )
    if answer is None:
        raise HTTPException(status_code=404, detail="question not found, not pending, or belongs to another tenant")
    return JSONResponse(content={"status": "ok", "tenant_id": scope, "answer": answer})
