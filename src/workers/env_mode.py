"""Environment mode helpers for governance behavior (prod/dev)."""

from __future__ import annotations

from typing import Any


def env_mode(settings: Any) -> str:
    raw = str(getattr(settings, "env_mode", "prod") or "prod").strip().lower()
    return "dev" if raw == "dev" else "prod"


def is_dev_mode(settings: Any) -> bool:
    return env_mode(settings) == "dev"


def is_prod_mode(settings: Any) -> bool:
    return env_mode(settings) == "prod"


def parse_allowed_namespaces(settings: Any) -> set[str]:
    raw = str(getattr(settings, "autonomous_allowed_namespaces", "multi-agent") or "")
    vals = {x.strip() for x in raw.split(",") if x.strip()}
    return vals or {"multi-agent"}


def namespace_allowed(settings: Any, namespace: str) -> bool:
    ns = str(namespace or "").strip()
    if not ns:
        return False
    return ns in parse_allowed_namespaces(settings)
