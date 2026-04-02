"""Shared proactive daemon models."""

from __future__ import annotations

from pydantic import BaseModel, Field

DEFAULT_RULE = "PrometheusProactiveThreshold"


class AnomalyEvent(BaseModel):
    trace_id: str = Field(min_length=4)
    rule_name: str = Field(default=DEFAULT_RULE)
    target: str = Field(default="cluster")
    namespace: str = Field(default="")
    metric_value: float = 0.0
    threshold: float = 0.0
    canonical_query: str = Field(min_length=1)
    timestamp: str = ""
    trigger_promql: str = Field(default="", description="PromQL instant đã kích hoạt (context).")
    error_hint: str = Field(default="", description="Taxonomy hint từ trigger (vd crash_loop_backoff).")
