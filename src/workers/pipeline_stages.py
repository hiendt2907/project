"""Back-compat shim — canonical home moved to pkg.observability.pipeline_stages.

Worker modules historically import ``from workers.pipeline_stages import mark_stage``.
The real implementation now lives under src/pkg/ so the gateway image can share it
without importing workers/. Keep this re-export to avoid touching every call site.
"""
from __future__ import annotations

from pkg.observability.pipeline_stages import (  # noqa: F401
    PIPELINE_STAGES,
    mark_stage,
)

__all__ = ["PIPELINE_STAGES", "mark_stage"]
