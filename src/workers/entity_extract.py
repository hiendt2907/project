"""Trích thực thể bằng helper LLM — JSON cố định (không chỉ regex)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

ENTITY_SCHEMA_HINT = """Trả về **một** JSON duy nhất, không markdown, không giải thích:
{"intent":"cpu|ram|disk|network|disk_io","namespace":"","pod_name":"","target_type":"pod|host"}
- intent: métric user muốn (mặc định cpu).
- namespace: rỗng nếu không nói.
- pod_name: tên pod đầy đủ hoặc workload; rỗng nếu không nói.
- target_type: "host" nếu user nói host/node/máy chủ/psutil; "pod" nếu pod/workload/container; ưu tiên theo câu hiện tại.
Nếu câu có dạng "namespace X pod Y" hoặc "pod Y namespace X" — tách đúng X vào namespace, Y vào pod_name (không nhét namespace vào pod_name)."""


async def extract_entities_llm(ctx: Any, user_text: str) -> dict[str, Any]:
    """Gọi model_helper — JSON entities."""
    settings = getattr(ctx, "settings", None)
    model = getattr(settings, "model_helper", None) or "qwen2.5:1.5b"
    llm = getattr(ctx, "llm", None)
    if llm is None or not (user_text or "").strip():
        return {}
    try:
        resp = await llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": ENTITY_SCHEMA_HINT},
                {"role": "user", "content": (user_text or "")[:4000]},
            ],
            options={"temperature": 0.0},
        )
        raw = ((resp.get("message") or {}).get("content") or "").strip()
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        blob = m.group(0) if m else raw
        data = json.loads(blob)
        if not isinstance(data, dict):
            return {}
        out: dict[str, Any] = {}
        for k in ("intent", "namespace", "pod_name", "target_type"):
            if k in data and data[k] is not None:
                out[k] = str(data[k]).strip()
        return out
    except Exception as e:
        logger.debug("extract_entities_llm: %s", e)
        return {}


def merge_llm_entities_into_slots(slots: dict[str, Any], ent: dict[str, Any]) -> dict[str, Any]:
    """Ưu tiên giá trị LLM không rỗng."""
    if not ent:
        return slots
    merged = dict(slots)
    for k in ("intent", "namespace", "pod_name", "target_type"):
        v = ent.get(k)
        if isinstance(v, str) and v.strip():
            merged[k] = v.strip().lower() if k == "target_type" else v.strip()
    return merged
