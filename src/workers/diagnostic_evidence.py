from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProbeRunRaw(BaseModel):
    probe_name: str
    status: Literal["PASSED", "FAILED", "INCONCLUSIVE", "SKIPPED"]
    raw_text: str = ""
    structured_hint: dict[str, Any] = Field(default_factory=dict)


class EvidenceObject(BaseModel):
    probe_name: str
    result: str
    raw_output: str = ""
    extracted_fact: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    ts: float = Field(default_factory=time.time)


def evidence_from_probe(raw: ProbeRunRaw, trace_id: str) -> EvidenceObject:
    fact: dict[str, Any] = {"status": raw.status}
    if raw.structured_hint:
        fact.update(raw.structured_hint)
    return EvidenceObject(
        probe_name=raw.probe_name,
        result=raw.status,
        raw_output=raw.raw_text[:4000],
        extracted_fact=fact,
        trace_id=trace_id,
    )
