"""Canonical hashing helpers shared by command producers and delivery records."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash for a typed command payload (no whitespace/order variance)."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["canonical_payload_hash"]
