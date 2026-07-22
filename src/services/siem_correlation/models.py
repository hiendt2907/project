"""Domain models — port of brain-go ``internal/domain`` (Incident/Entity) và
``internal/extract`` (KillChainStage). Immutable theo chuẩn dự án."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import NamedTuple

SCHEMA_VERSION = "1.0.0"
CHAIN_SCHEMA_VERSION = "1.0.0"

# Entity types — stable string constants used as Redis key segments and as the
# `type` field in the correlation-chain contract. Do NOT rename without a
# schema bump (parity with domain.EntityType).
ENTITY_IP = "ip"
ENTITY_USER = "user"
ENTITY_SESSION = "session_id"
ENTITY_HOST = "host"
ENTITY_POD = "pod"
ENTITY_PROCESS = "process"


class Entity(NamedTuple):
    """One correlation dimension. Carries only a parsed/normalized field value
    (an IP, a username, ...) — never a raw line of VM log content."""

    type: str
    value: str


class KillChainStage(NamedTuple):
    """Coarse, ordered position in an intrusion kill chain (order drives the
    sequence signal)."""

    name: str
    order: int


class IncidentMeta(NamedTuple):
    """Metadata-only snapshot persisted per incident to build a chain later.
    NO raw VM data — only parsed/derived fields (parity: correlate.incidentMeta)."""

    id: str
    category: str
    severity: str
    source_ip: str
    stage: KillChainStage
    ts: int


@dataclass(frozen=True)
class Incident:
    """Internal representation of the actionable incident contract
    (parity: domain.Incident, Kafka-decode subset)."""

    incident_id: str
    tenant_id: str
    severity: str
    source: str
    category: str
    timestamp_unix: int
    schema_version: str
    rule_id: str = ""
    source_ip: str = ""
    dest_ip: str = ""
    raw_log: str = ""
    correlation_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    entities: tuple[Entity, ...] = ()

    def with_entities(self, entities: tuple[Entity, ...]) -> "Incident":
        return dataclasses.replace(self, entities=entities)
