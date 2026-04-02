"""Postgres HA `itops_sop_ledger` helpers — payload khớp try_fast_path."""

from __future__ import annotations

import json
import uuid
from typing import Any

from rag.pgvector_store import COLLECTION_SOP

# Re-export for ingest
SOP_COLLECTION = COLLECTION_SOP


def sop_point_id(*, template_id: str, variant_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sop:{template_id}:{variant_key}"))


def sop_payload_for_fast_path(
    *,
    match_text: str,
    tool: str,
    args: dict[str, Any],
    auto_execute: bool,
    template_id: str,
    variant_key: str,
) -> dict[str, Any]:
    return {
        "sop_id": f"{template_id}:{variant_key[:48]}",
        "template_id": template_id,
        "variant_key": variant_key,
        "match_text": match_text[:8000],
        "tool": tool,
        "args": args,
        "auto_execute": bool(auto_execute),
    }


def canonical_variant_key(slot_values: dict[str, str]) -> str:
    return json.dumps(slot_values, sort_keys=True, ensure_ascii=False)
