"""Shared proactive daemon models."""

from __future__ import annotations

from typing import Optional

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
    symptom_group: str = Field(default="", description="Aligned with omni.io/symptom-group / dispatcher.")
    drift_type: str = Field(default="")
    deployment: str = Field(default="")
    omni_layer: str = Field(default="", description="infra|security|workload when present on alert.")
    omni_verify_required: Optional[bool] = Field(
        default=None,
        description="From alert label omni_verify_required; None = not set (use global defaults).",
    )
    gigo_metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Early GIGO: namespace/pod/deployment/error_code/… from Prometheus labels (pkg.autonomy.gigo).",
    )
