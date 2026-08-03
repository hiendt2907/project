"""Pydantic v2 schema for remote agent evidence envelopes pushed to /agent/v1/push."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from pkg.domain.taxonomy import lane_to_domain, normalize_domain


class SourceType(str, Enum):
    k8s = "k8s"
    linux_host = "linux_host"
    windows_host = "windows_host"
    network_device = "network_device"
    database = "database"
    cache = "cache"
    queue = "queue"
    storage = "storage"
    vm = "vm"
    hypervisor = "hypervisor"
    custom = "custom"


class EvidenceType(str, Enum):
    metrics = "metrics"
    log_event = "log_event"
    alert = "alert"
    custom_check = "custom_check"


class StreamTag(str, Enum):
    SYS_RESOURCE = "SYS_RESOURCE"
    SYS_HARD_FAIL = "SYS_HARD_FAIL"
    APP_HTTP = "APP_HTTP"
    SIEM_SECURITY = "SIEM_SECURITY"
    custom = "custom"


class AgentEvidenceEnvelope(BaseModel):
    schema_version: str = "1.0"
    tenant_id: str = Field(
        pattern=r"^[a-zA-Z0-9_-]{1,64}$",
        description="Alphanumeric + hyphen/underscore, 1-64 chars",
    )
    agent_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="UUID4 format",
    )
    agent_version: str = "0.1.0"
    source_type: SourceType
    target_id: str = Field(
        description="e.g. 'host:web-01.acme.internal'",
    )
    timestamp: str = Field(description="ISO8601 timestamp")
    trace_id: str = Field(
        pattern=r"^[a-zA-Z0-9_-]{8,128}$",
        description="Alphanumeric + hyphen/underscore, 8-128 chars",
    )
    sequence_no: int = Field(ge=0)
    evidence_type: EvidenceType
    stream_tags: list[StreamTag] = Field(min_length=1, max_length=4)
    payload: dict[str, Any]
    domain: str = Field(
        default="",
        description=(
            "Canonical domain (pkg.domain.taxonomy). Optional on the wire: agents "
            "older than the domain rollout do not send it, so it is inferred here."
        ),
    )

    @model_validator(mode="after")
    def _resolve_domain(self) -> "AgentEvidenceEnvelope":
        """Fill/normalise ``domain`` — READ path, so never raise.

        Precedence: explicit ``domain`` > ``payload["domain"]`` (collector wrote it
        inside the evidence envelope) > inferred from the deprecated lane axis.
        Old agents keep working; they just land on the lane-derived value, which is
        ``unknown`` for ``SYS_HARD_FAIL`` because that lane carried four different
        real domains and guessing one would be worse than admitting we don't know.
        """
        candidates = (self.domain, str(self.payload.get("domain") or ""))
        for c in candidates:
            d = normalize_domain(c)
            if d != "unknown":
                object.__setattr__(self, "domain", d)
                return self

        lane = str(self.payload.get("lane") or "") or (
            self.stream_tags[0].value if self.stream_tags else ""
        )
        object.__setattr__(self, "domain", lane_to_domain(lane))
        return self
