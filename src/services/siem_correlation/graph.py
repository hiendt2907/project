"""Redis-backed union-find graph correlator.

Port 1-1 của brain-go ``internal/correlate/graph.go`` +
``chain.go::maybeEmitGraphChain``. Links incidents sharing one or more
entities (ip/user/session/host/pod/process) into a connected component; when
the component crosses all gates (threshold, entity span, confidence) one
CorrelationChain is emitted (deduped per component root).

TENANT ISOLATION INVARIANT: every Redis key is namespaced by tenant; the
union-find never merges nodes across tenants.

Key layout (identical to Go, prefix configurable for parity runs):
    {P}ent:<tenant>:<type>:<value>   ZSET member=incident_id score=ts
    {P}uf:<tenant>                   HASH node → parent
    {P}ginc:<tenant>:<root>          ZSET member=incident_id score=ts
    {P}gent:<tenant>:<root>          SET of entity nodes
    {P}imeta:<tenant>:<id>           HASH incident metadata
    {P}gdedup:<tenant>:<root>        dedup marker (SETNX + TTL)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from services.siem_correlation import config as corr_config
from services.siem_correlation.chain import build_chain, validate_chain
from services.siem_correlation.confidence import score_chain
from services.siem_correlation.entities import stage_for
from services.siem_correlation.models import (
    Entity,
    Incident,
    IncidentMeta,
    KillChainStage,
)

# Safety bound on union-find parent traversal (parity: ufMaxDepth).
_UF_MAX_DEPTH = 32
# Cap correlation members to avoid oversized chains (parity: maxChainIDs).
_MAX_CHAIN_IDS = 20
# Key TTL slack past the sliding window (parity: graphLink ttl).
_TTL_SLACK_SECONDS = 120


@dataclass(frozen=True)
class GraphConfig:
    window_seconds: int
    threshold: int
    dedup_seconds: int
    min_entity_span: int
    min_confidence: float
    w_entity: float
    w_sequence: float
    w_volume: float
    key_prefix: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "GraphConfig":
        return cls(
            window_seconds=corr_config.corr_window_seconds(env),
            threshold=corr_config.corr_threshold(env),
            dedup_seconds=corr_config.corr_dedup_seconds(env),
            min_entity_span=corr_config.corr_min_entity_span(env),
            min_confidence=corr_config.corr_min_confidence(env),
            w_entity=corr_config.corr_weight_entity(env),
            w_sequence=corr_config.corr_weight_sequence(env),
            w_volume=corr_config.corr_weight_volume(env),
            key_prefix=corr_config.key_prefix(env),
        )


def _node_key(entity: Entity) -> str:
    return f"{entity.type}:{entity.value}"


class GraphCorrelator:
    """Async port of the Go graph correlator. ``process`` links one incident
    into the entity graph and returns the CorrelationChain envelope when the
    component crosses all gates, else None."""

    def __init__(self, redis: Any, cfg: GraphConfig) -> None:
        self._redis = redis
        self._cfg = cfg

    # -- key helpers --------------------------------------------------------
    def _k_ent(self, tenant: str, node: str) -> str:
        return f"{self._cfg.key_prefix}ent:{tenant}:{node}"

    def _k_uf(self, tenant: str) -> str:
        return f"{self._cfg.key_prefix}uf:{tenant}"

    def _k_ginc(self, tenant: str, root: str) -> str:
        return f"{self._cfg.key_prefix}ginc:{tenant}:{root}"

    def _k_gent(self, tenant: str, root: str) -> str:
        return f"{self._cfg.key_prefix}gent:{tenant}:{root}"

    def _k_imeta(self, tenant: str, incident_id: str) -> str:
        return f"{self._cfg.key_prefix}imeta:{tenant}:{incident_id}"

    def _k_gdedup(self, tenant: str, root: str) -> str:
        return f"{self._cfg.key_prefix}gdedup:{tenant}:{root}"

    # -- public API ---------------------------------------------------------
    async def process(self, inc: Incident, *, now: int | None = None) -> dict[str, Any] | None:
        """maybeEmitGraphChain: graph-link, gate, dedup, build chain."""
        now = int(time.time()) if now is None else now
        root, members = await self._graph_link(inc, now)
        if not root or len(members) < self._cfg.threshold:
            return None

        entities = await self._component_entities(inc.tenant_id, root)
        if len({e.type for e in entities}) < self._cfg.min_entity_span:
            return None

        signals = score_chain(
            members,
            entities,
            threshold=self._cfg.threshold,
            w_entity=self._cfg.w_entity,
            w_sequence=self._cfg.w_sequence,
            w_volume=self._cfg.w_volume,
        )
        if signals["confidence"] < self._cfg.min_confidence:
            return None

        # Dedup per component root (SETNX + TTL).
        acquired = await self._redis.set(
            self._k_gdedup(inc.tenant_id, root), now, nx=True, ex=self._cfg.dedup_seconds
        )
        if not acquired:
            return None

        chain = build_chain(
            inc.tenant_id, members, entities, signals,
            window_seconds=self._cfg.window_seconds, now=now,
        )
        validate_chain(chain)
        return chain

    # -- graph internals ----------------------------------------------------
    async def _graph_link(self, inc: Incident, now: int) -> tuple[str, list[IncidentMeta]]:
        """Process one incident through the union-find graph; returns the
        component root plus distinct in-window member metadata."""
        tenant = inc.tenant_id.strip()
        if not tenant or not inc.entities:
            return "", []
        window_start = now - self._cfg.window_seconds
        ttl = self._cfg.window_seconds + _TTL_SLACK_SECONDS

        await self._store_meta(tenant, inc, now, ttl)

        # Register this incident under each of its entity windows.
        nodes: list[str] = []
        for entity in inc.entities:
            node = _node_key(entity)
            nodes.append(node)
            wk = self._k_ent(tenant, node)
            pipe = self._redis.pipeline()
            pipe.zadd(wk, {inc.incident_id: now})
            pipe.zremrangebyscore(wk, "-inf", window_start)
            pipe.expire(wk, ttl)
            await pipe.execute()

        # Union all of this incident's entity nodes together (they co-occur).
        uf_key = self._k_uf(tenant)
        root_node = nodes[0]
        await self._redis.hsetnx(uf_key, root_node, root_node)
        r0 = await self._find(uf_key, root_node)
        for node in nodes[1:]:
            await self._redis.hsetnx(uf_key, node, node)
            rn = await self._find(uf_key, node)
            if rn != r0:
                # Merge: keep the lexicographically smaller root (deterministic).
                if rn < r0:
                    r0, rn = rn, r0
                await self._redis.hset(uf_key, rn, r0)
                await self._merge_groups(tenant, winner=r0, loser=rn, ttl=ttl)
        await self._redis.expire(uf_key, ttl)

        root = await self._find(uf_key, root_node)

        # Add incident + its entity nodes to the component group.
        g_inc = self._k_ginc(tenant, root)
        g_ent = self._k_gent(tenant, root)
        pipe = self._redis.pipeline()
        pipe.zadd(g_inc, {inc.incident_id: now})
        pipe.zremrangebyscore(g_inc, "-inf", window_start)
        pipe.expire(g_inc, ttl)
        for node in nodes:
            pipe.sadd(g_ent, node)
        pipe.expire(g_ent, ttl)
        await pipe.execute()

        ids = await self._redis.zrevrange(g_inc, 0, _MAX_CHAIN_IDS - 1)
        members = await self._load_metas(tenant, ids)
        return root, members

    async def _store_meta(self, tenant: str, inc: Incident, now: int, ttl: int) -> None:
        stage = stage_for(inc)
        key = self._k_imeta(tenant, inc.incident_id)
        await self._redis.hset(key, mapping={
            "category": inc.category,
            "severity": inc.severity,
            "source_ip": inc.source_ip,
            "stage_name": stage.name,
            "stage_order": stage.order,
            "ts": now,
        })
        await self._redis.expire(key, ttl)

    async def _load_metas(self, tenant: str, ids: list[str]) -> list[IncidentMeta]:
        out: list[IncidentMeta] = []
        for incident_id in ids:
            h = await self._redis.hgetall(self._k_imeta(tenant, incident_id))
            if not h:
                continue  # meta expired — skip, parity with Go loadMetas
            out.append(IncidentMeta(
                id=incident_id,
                category=h.get("category", ""),
                severity=h.get("severity", ""),
                source_ip=h.get("source_ip", ""),
                stage=KillChainStage(
                    name=h.get("stage_name", ""),
                    order=_to_int(h.get("stage_order")),
                ),
                ts=_to_int(h.get("ts")),
            ))
        return out

    async def _find(self, uf_key: str, node: str) -> str:
        """Resolve a node to its component root, depth-bounded, with
        best-effort path compression on the final hop."""
        cur = node
        for _ in range(_UF_MAX_DEPTH):
            parent = await self._redis.hget(uf_key, cur)
            if parent is None:
                return cur
            if parent == cur or parent == "":
                if cur != node:
                    await self._redis.hset(uf_key, node, cur)
                return cur
            cur = parent
        return cur

    async def _merge_groups(self, tenant: str, *, winner: str, loser: str, ttl: int) -> None:
        """Fold the loser root's incidents and entities into the winner."""
        if winner == loser:
            return
        l_inc = self._k_ginc(tenant, loser)
        w_inc = self._k_ginc(tenant, winner)
        l_ent = self._k_gent(tenant, loser)
        w_ent = self._k_gent(tenant, winner)

        entries = await self._redis.zrange(l_inc, 0, -1, withscores=True)
        pipe = self._redis.pipeline()
        for member, score in entries:
            pipe.zadd(w_inc, {member: score})
        loser_entities = await self._redis.smembers(l_ent)
        if loser_entities:
            pipe.sadd(w_ent, *loser_entities)
        pipe.expire(w_inc, ttl)
        pipe.expire(w_ent, ttl)
        pipe.delete(l_inc, l_ent)
        await pipe.execute()

    async def _component_entities(self, tenant: str, root: str) -> list[Entity]:
        nodes = await self._redis.smembers(self._k_gent(tenant, root))
        out: list[Entity] = []
        for node in nodes:
            typ, sep, value = node.partition(":")
            if sep and typ:
                out.append(Entity(type=typ, value=value))
        return sorted(out)


def _to_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
