"""Parse MACHINE_JSON + HUMAN_SUMMARY from SDK-only (RAG-miss) analyst replies."""

from __future__ import annotations

import json
from typing import Any

_MACHINE_JSON_MAX_CHARS = 600


def _first_json_object(s: str) -> dict[str, Any] | None:
    s = s.strip()
    if not s.startswith("{"):
        i = s.find("{")
        if i < 0:
            return None
        s = s[i:]
    depth = 0
    end = -1
    for i, c in enumerate(s):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    chunk = s[:end]
    if len(chunk) > _MACHINE_JSON_MAX_CHARS:
        chunk = chunk[:_MACHINE_JSON_MAX_CHARS]
    try:
        o = json.loads(chunk)
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def parse_two_channel_sdk_only(text: str) -> dict[str, Any]:
    """
    Expected format:
      MACHINE_JSON: {...}
      HUMAN_SUMMARY: words (max 30 enforced by caller truncate)
    """
    raw = (text or "").strip()
    if not raw:
        return {"machine": None, "human": "", "raw": raw}

    machine: dict[str, Any] | None = None
    human = ""

    if "MACHINE_JSON:" in raw and "HUMAN_SUMMARY:" in raw:
        pre, post = raw.split("HUMAN_SUMMARY:", 1)
        human = post.strip()
        if "MACHINE_JSON:" in pre:
            mj = pre.split("MACHINE_JSON:", 1)[1].strip()
            machine = _first_json_object(mj)
    else:
        human = raw

    return {"machine": machine, "human": human, "raw": raw}
