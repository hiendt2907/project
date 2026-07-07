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
import re
import time
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Mermaid node shape (open, close) by entity-type prefix — see aoip.system_graph
# NODE_TYPE_PREFIX for the canonical type->prefix mapping. Distinct shapes make
# the topology diagram readable at a glance (host vs service vs api vs db vs doc).
_TOPOLOGY_NODE_SHAPES: dict[str, tuple[str, str]] = {
    "host": ("([", "])"),  # stadium
    "svc": ("[", "]"),  # rectangle
    "api": ("{{", "}}"),  # hexagon
    "db": ("[(", ")]"),  # cylinder
    "doc": ("(", ")"),  # rounded
}
_TOPOLOGY_DEFAULT_SHAPE = ("[", "]")

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
    Omni keeps only a reference (hash + length), never the text itself.

    Agents ≥1.2.0 hash at the source and send content_hash/content_length
    directly (no raw content). Legacy agents still send raw ``content``: keep
    hashing it here as a defensive fallback during the transition window, but
    flag it — raw content crossing the wire violates INV_DATA_RESIDENCY."""
    out: list[dict[str, Any]] = []
    for doc in documents:
        if "content" not in doc and doc.get("content_hash"):
            entry = {
                "path": doc.get("path"),
                "content_hash": doc.get("content_hash"),
                "content_length": int(doc.get("content_length") or 0),
            }
            if doc.get("mtime") is not None:
                entry["mtime"] = doc.get("mtime")
            out.append(entry)
            continue
        content = str(doc.get("content") or "")
        if content:
            logger.warning(
                "[residency] legacy agent sent raw doc content path=%s len=%d — "
                "hashed on arrival; upgrade agent to hash at source",
                doc.get("path"), len(content),
            )
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


# OS/init-system noise — always present on any Linux host, never part of the
# customer's actual system architecture. Filtered from diagram rendering only
# (raw evidence/residency storage is untouched — this is a display concern).
_NOISE_NAME_PATTERN = re.compile(
    r"^(systemd.*|dbus.*|rpc[-.].*|rpcbind|nfs.*|cron|rsyslog.*|console-getty|"
    r"agetty|user@\d+|\(sd-pam\)|orbstack-agent:?|omni-remote-agent.*|ps)$",
    re.IGNORECASE,
)


def _is_noise_component(name: str) -> bool:
    return bool(_NOISE_NAME_PATTERN.match(name.strip()))


# Kernel-owned ports with no resolvable userspace process name in `ss` output
# (e.g. NFS server/rpcbind's ephemeral callback ports) — same noise, no name to match on.
_NOISE_PORTS = {111, 2049}


def _is_noise_port(port: dict[str, Any]) -> bool:
    if _is_noise_component(str(port.get("service") or "")):
        return True
    return port.get("port") in _NOISE_PORTS


# Architecture-tier classification by real process/service name — never guessed
# per-tenant, just a fixed vocabulary of well-known edge/data daemons so the
# topology/API diagrams can lay out as Edge -> Application -> Data (the
# convention system/network architects actually draw), same tiers a human
# reviewing `ss -tnp` output would assign by binary name alone.
_EDGE_TIER_NAMES = {
    "nginx", "haproxy", "traefik", "envoy", "httpd", "apache2", "caddy", "kong",
}
_DATA_TIER_NAMES = {
    "mariadbd", "mysqld", "mysql", "postgres", "postgresql", "redis-server",
    "redis", "mongod", "mongodb", "memcached", "valkey", "cassandra",
}


def _service_tier(name: str) -> str:
    base = name.strip().lower()
    if base in _EDGE_TIER_NAMES:
        return "edge"
    if base in _DATA_TIER_NAMES:
        return "data"
    return "app"


_TIER_ORDER = ("edge", "app", "data", "unclassified")
_TIER_TITLE = {
    "edge": "Edge / Gateway",
    "app": "Application",
    "data": "Data",
    "unclassified": "Unclassified",
}

# Mermaid classDef per tier — draw.io-style colour-coded boxes so a reader
# tells Edge/App/Data apart at a glance instead of every node rendering as the
# same undifferentiated dark rectangle. Muted `_TIER_CLUSTER_STYLE` tints the
# subgraph container itself; the brighter `_TIER_NODE_STYLE` is the node fill,
# so a node visibly "sits inside" its tier's cluster colour.
_TIER_NODE_STYLE = {
    "edge": "fill:#123c4d,stroke:#4fc3f7,stroke-width:2px,color:#e0f7fa",
    "app": "fill:#2b1f45,stroke:#b388ff,stroke-width:2px,color:#ede7f6",
    "data": "fill:#402712,stroke:#ffb74d,stroke-width:2px,color:#fff3e0",
    "unclassified": "fill:#262626,stroke:#9e9e9e,stroke-width:2px,color:#eeeeee",
}
_TIER_CLUSTER_STYLE = {
    "edge": "fill:#0d232b,stroke:#1f5a6e,stroke-width:1.5px,color:#7fd8f0",
    "app": "fill:#1c1430,stroke:#4527a0,stroke-width:1.5px,color:#cbb6ff",
    "data": "fill:#2b1a0c,stroke:#8d5524,stroke-width:1.5px,color:#ffcc80",
    "unclassified": "fill:#1a1a1a,stroke:#555555,stroke-width:1.5px,color:#bbbbbb",
}
# Clear, high-contrast connector line — draw.io-style edges instead of
# Mermaid's near-invisible default 1px light-grey line on a dark background.
_LINK_STYLE = "stroke:#8ecae6,stroke-width:2px"


def render_component_diagram(doc: dict[str, Any]) -> str:
    services = (doc.get("service_topology") or {}).get("services") or []
    lines = ["graph TD"]
    real_services = [s for s in services if not _is_noise_component(str(s.get("name") or ""))]
    if not real_services:
        lines.append("  host[\"host\"]")
    for svc in real_services[:50]:
        name = str(svc.get("name") or "svc").replace('"', "'")
        node_id = f"svc_{abs(hash(name)) % 100000}"
        lines.append(f'  {node_id}["{name}"]')
    return "\n".join(lines)


def render_api_sequence_diagram(doc: dict[str, Any]) -> str:
    """Client -> [Gateway ->] exposed-port fan-out. Deliberately a `graph LR`,
    not a `sequenceDiagram`: this data has no real temporal/causal order (it's
    a flat port_scan snapshot), and Mermaid draws every sequence-diagram actor
    box twice (top + bottom of the lifeline) — for 10+ ports that renders as
    a wall of duplicated boxes with no information gain over a plain graph.

    When one of the discovered ports is a known edge/gateway process (nginx,
    haproxy, ...) the diagram draws the real API-gateway pattern architects
    expect: Client -> Gateway -> backend services, gateway inferred from the
    actual discovered process name — never asserted when nothing edge-tier
    was observed (falls back to a flat Client fan-out)."""
    all_ports = (doc.get("port_scan") or {}).get("listening_ports") or []
    ports = [p for p in all_ports if not _is_noise_port(p)]
    lines = ["graph LR", '  client(["Client"])']
    style_lines = [f"  classDef clientNode {_TIER_CLUSTER_STYLE['unclassified']}", "  class client clientNode"]
    if not ports:
        lines.append('  none["(no listening ports discovered yet)"]')
        lines.append("  client --> none")
        return "\n".join(lines + style_lines)

    deduped: list[dict[str, Any]] = []
    seen_ports: set[int] = set()
    for p in ports:
        port = p.get("port")
        if port in seen_ports:
            continue
        seen_ports.add(port)
        deduped.append(p)
        if len(seen_ports) > 20:
            break

    gateway = next(
        (p for p in deduped if _service_tier(str(p.get("service") or "")) == "edge"), None,
    )
    style_lines.append(f"  classDef appNode {_TIER_NODE_STYLE['app']}")
    style_lines.append(f"  linkStyle default {_LINK_STYLE}")
    if gateway is None:
        svc_ids = []
        for p in deduped:
            port = p.get("port")
            service = str(p.get("service") or f"port-{port}").replace('"', "'")
            node_id = f"svc_{port}"
            lines.append(f'  {node_id}["{service} ({port})"]')
            lines.append(f"  client --> {node_id}")
            svc_ids.append(node_id)
        style_lines.append(f"  class {','.join(svc_ids)} appNode")
        return "\n".join(lines + style_lines)

    gw_port = gateway.get("port")
    gw_service = str(gateway.get("service") or f"port-{gw_port}").replace('"', "'")
    gw_id = f"svc_{gw_port}"
    lines.append(f'  {gw_id}{{{{"{gw_service} ({gw_port}) — Gateway"}}}}')
    lines.append(f"  client --> {gw_id}")
    svc_ids = []
    for p in deduped:
        if p is gateway:
            continue
        port = p.get("port")
        service = str(p.get("service") or f"port-{port}").replace('"', "'")
        node_id = f"svc_{port}"
        lines.append(f'  {node_id}["{service} ({port})"]')
        lines.append(f"  {gw_id} --> {node_id}")
        svc_ids.append(node_id)
    style_lines.append(f"  classDef gatewayNode {_TIER_NODE_STYLE['edge']}")
    style_lines.append(f"  class {gw_id} gatewayNode")
    if svc_ids:
        style_lines.append(f"  class {','.join(svc_ids)} appNode")
    return "\n".join(lines + style_lines)


def render_business_flow_diagram(doc: dict[str, Any]) -> str:
    all_processes = (doc.get("process_list") or {}).get("processes") or []
    processes = [p for p in all_processes if not _is_noise_component(str(p.get("name") or ""))]
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


def _topology_node_shape(entity_id: str) -> tuple[str, str]:
    node_type = entity_id.split(":", 1)[0] if ":" in entity_id else ""
    return _TOPOLOGY_NODE_SHAPES.get(node_type, _TOPOLOGY_DEFAULT_SHAPE)


def _topology_node_id(entity_id: str) -> str:
    return f"n_{abs(hash(entity_id)) % 100000}"


def _entity_label(entity_id: str) -> str:
    return entity_id.split(":", 1)[-1] if ":" in entity_id else entity_id


def _majority_tier(services: list[str]) -> str:
    tiers = [_service_tier(_entity_label(svc)) for svc in services]
    if not tiers:
        return "unclassified"
    counts = {t: tiers.count(t) for t in set(tiers)}
    best = max(counts.values())
    for tier in _TIER_ORDER:
        if counts.get(tier) == best:
            return tier
    return "unclassified"


def render_system_topology_diagram(edges: Sequence[Any]) -> str:
    """Render relational Facts (connects_to/hosts/proxies_to/depends_on/... —
    see ``aoip.objects.RELATIONAL_PREDICATES``) as a layered architecture
    diagram — the convention system/network architects actually draw: one
    subgraph per tier (Edge/Gateway -> Application -> Data, top to bottom),
    each real (non-noise) service placed under its tier with the host it runs
    on in the label, and cross-host ``connects_to``/other predicate edges
    drawn straight to the specific real service(s) on each side — never to a
    placeholder node duplicating a group label (Mermaid does not need one: an
    edge can target a subgraph id directly, landing on its cluster boundary),
    so a single glance shows which tier talks to which."""
    lines = ["graph TB"]
    if not edges:
        lines.append('  empty(["no relational facts yet — waiting for connection_scan"])')
        return "\n".join(lines)

    host_services: dict[str, list[str]] = {}
    cross_edges: list[Any] = []
    for fact in edges[:200]:
        if fact.predicate == "hosts" and fact.subject.startswith("host:"):
            svc = fact.obj
            if _is_noise_component(_entity_label(svc)):
                continue
            host_services.setdefault(fact.subject, [])
            if svc not in host_services[fact.subject]:
                host_services[fact.subject].append(svc)
        else:
            cross_edges.append(fact)

    seen: dict[str, str] = {}

    def node_for(entity_id: str) -> str:
        if entity_id not in seen:
            seen[entity_id] = _topology_node_id(entity_id)
        return seen[entity_id]

    all_hosts = sorted({*host_services.keys(), *(f.subject for f in cross_edges if f.subject.startswith("host:")),
                         *(f.obj for f in cross_edges if f.obj.startswith("host:"))})
    declared: set[str] = set()
    # entity_id (host, or ambiguous-host's group) -> id of the node/subgraph an
    # edge should actually touch.
    endpoint_id_of: dict[str, str] = {}
    host_tier: dict[str, str] = {}
    tier_bodies: dict[str, list[str]] = {tier: [] for tier in _TIER_ORDER}
    # node ids placed inside each tier — used below to emit a `classDef` +
    # `class` colour assignment per tier (draw.io-style colour-coded boxes),
    # separate from `tier_bodies` (raw Mermaid node/subgraph declaration text).
    tier_node_ids: dict[str, list[str]] = {tier: [] for tier in _TIER_ORDER}

    for host in all_hosts:
        services = host_services.get(host, [])
        host_label = _entity_label(host)
        if not services:
            host_id = node_for(host)
            open_shape, close_shape = _topology_node_shape(host)
            tier_bodies["unclassified"].append(f'    {host_id}{open_shape}"{host_label}"{close_shape}')
            endpoint_id_of[host] = host_id
            host_tier[host] = "unclassified"
            tier_node_ids["unclassified"].append(host_id)
        elif len(services) == 1:
            svc = services[0]
            svc_id = node_for(svc)
            declared.add(svc)
            tier = _service_tier(_entity_label(svc))
            open_shape, close_shape = _topology_node_shape(svc)
            tier_bodies[tier].append(f'    {svc_id}{open_shape}"{_entity_label(svc)} ({host_label})"{close_shape}')
            endpoint_id_of[host] = svc_id
            host_tier[host] = tier
            tier_node_ids[tier].append(svc_id)
        else:
            # 2+ services, ambiguous which one a host-level connects_to edge
            # belongs to — group them under one sub-cluster (never fan out
            # the edge to every service: that would assert "all of them talk
            # to the peer", which connection_scan never actually told us).
            host_id = node_for(host)
            group_id = f"{host_id}_group"
            endpoint_id_of[host] = group_id
            tier = _majority_tier(services)
            body = [f'    subgraph {group_id} ["{host_label}"]']
            for svc in services:
                svc_id = node_for(svc)
                declared.add(svc)
                open_shape, close_shape = _topology_node_shape(svc)
                body.append(f'      {svc_id}{open_shape}"{_entity_label(svc)}"{close_shape}')
                tier_node_ids[tier].append(svc_id)
            body.append("    end")
            tier_bodies[tier].append("\n".join(body))
            host_tier[host] = tier

    cluster_ids: dict[str, list[str]] = {tier: [] for tier in _TIER_ORDER}
    for tier in _TIER_ORDER:
        body = tier_bodies[tier]
        if not body:
            continue
        cluster_id = f"tier_{tier}"
        cluster_ids[tier].append(cluster_id)
        lines.append(f'  subgraph {cluster_id} ["{_TIER_TITLE[tier]}"]')
        lines.extend(body)
        lines.append("  end")

    def endpoint(entity_id: str) -> str:
        return endpoint_id_of.get(entity_id, node_for(entity_id))

    def tier_rank(entity_id: str) -> int:
        tier = host_tier.get(entity_id, "unclassified")
        return _TIER_ORDER.index(tier) if tier in _TIER_ORDER else len(_TIER_ORDER)

    # connection_scan observes each TCP connection from both ends, so the
    # same logical link often arrives twice as reciprocal facts (A connects_to
    # B, and B connects_to A). Rendering both would draw a 2-cycle between the
    # same pair of nodes, which breaks Mermaid's dagre top-to-bottom ranking
    # and silently flips the Edge->App->Data tier order. Collapse each
    # unordered (subject, obj, predicate) pair to one edge, oriented from the
    # lower tier rank to the higher one so the layered layout stays intact —
    # this discards no information: both facts already assert the same link.
    seen_pairs: set[tuple[frozenset[str], str]] = set()
    for fact in cross_edges:
        pair_key = (frozenset((fact.subject, fact.obj)), fact.predicate)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        subject, obj = fact.subject, fact.obj
        if tier_rank(subject) > tier_rank(obj):
            subject, obj = obj, subject

        src = endpoint(subject) if subject.startswith("host:") else node_for(subject)
        dst = endpoint(obj) if obj.startswith("host:") else node_for(obj)
        for entity_id, node_id in ((subject, src), (obj, dst)):
            # Entities referenced only by a cross-edge (never placed in a
            # tier subgraph — e.g. an external/unresolved peer, or a document
            # node) still need their own node declared exactly once.
            if entity_id.startswith("host:") or entity_id in declared:
                continue
            declared.add(entity_id)
            open_shape, close_shape = _topology_node_shape(entity_id)
            lines.append(f'  {node_id}{open_shape}"{_entity_label(entity_id)}"{close_shape}')
        lines.append(f"  {src} -->|{fact.predicate}| {dst}")

    # draw.io-style colour coding: each tier's cluster boundary gets a muted
    # tint, its nodes a brighter fill of the same hue, and connectors a
    # clearly visible line — instead of every box rendering as the same
    # undifferentiated dark rectangle.
    lines.append(f"  linkStyle default {_LINK_STYLE}")
    for tier in _TIER_ORDER:
        if cluster_ids[tier]:
            lines.append(f"  style {cluster_ids[tier][0]} {_TIER_CLUSTER_STYLE[tier]}")
        if tier_node_ids[tier]:
            class_name = f"tierNode{tier.capitalize()}"
            lines.append(f"  classDef {class_name} {_TIER_NODE_STYLE[tier]}")
            lines.append(f"  class {','.join(tier_node_ids[tier])} {class_name}")
    return "\n".join(lines)


def render_all_diagrams(doc: dict[str, Any], edges: Sequence[Any] = ()) -> str:
    return "\n\n".join(
        [
            "%% component architecture",
            render_component_diagram(doc),
            "%% API sequence",
            render_api_sequence_diagram(doc),
            "%% business flow",
            render_business_flow_diagram(doc),
            "%% system topology (cross-host/cross-entity relational facts)",
            render_system_topology_diagram(edges),
        ]
    )


async def regenerate_diagrams(redis: Any, tenant_id: str) -> int:
    """A3: render 4 Mermaid diagram types from the accumulated doc + persisted
    SystemModel relational facts, save as a new immutable version (never
    overwrite — diffable history). Returns the new version."""
    doc = await get_accumulated_doc(redis, tenant_id)
    from aoip.system_model_store import load_system_model

    model, _revision = await load_system_model(redis, tenant_id)
    rendered = render_all_diagrams(doc, edges=model.edges)
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


async def compute_business_flow_pct(redis: Any, tenant_id: str, doc: dict[str, Any]) -> float:
    """A service counts as "confirmed" if EITHER the tenant-authored discovery
    doc marks it described (machine-set, see ``_sanitize_services``) OR the
    Entity Competency Matrix (aoip.competency_matrix) has a CLAIMED/VERIFIED
    ``business_capability`` facet for it — i.e. answering a Human Claim
    question about a service's business purpose (Slice O2B) now moves this
    percentage, closing the readiness-gate/competency disconnect found in
    iteration 15 (see docs/product/PRODUCT_PROOF.md)."""
    services = (doc.get("service_topology") or {}).get("services") or []
    if not services:
        return 0.0
    from aoip.claims_store import load_claims
    from aoip.competency_matrix import FacetState, build_entity_competency
    from aoip.system_model_store import load_contradictions, load_system_model

    model, _revision = await load_system_model(redis, tenant_id)
    contradictions = await load_contradictions(redis, tenant_id)
    claims = await load_claims(redis, tenant_id)

    confirmed = 0
    for svc in services:
        if svc.get("described"):
            confirmed += 1
            continue
        name = svc.get("name")
        if not name:
            continue
        comp = build_entity_competency(
            model, contradictions, entity_type="service", entity_id=f"svc:{name}", claims=claims,
        )
        if comp.facet("business_capability").state in (FacetState.CLAIMED, FacetState.VERIFIED):
            confirmed += 1
    return round(100.0 * confirmed / len(services), 2)


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
    business_flow_confirmed_pct = await compute_business_flow_pct(redis, tenant_id, doc)
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
