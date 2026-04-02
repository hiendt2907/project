"""Loki-friendly one-line JSON logs cho ReAct (reasoning_path)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_react_json(reasoning_path: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"reasoning_path": reasoning_path, **fields}
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    logger.info("%s", line)
