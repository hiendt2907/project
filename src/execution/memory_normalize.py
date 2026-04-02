"""Canonical symptom text, workload fingerprint, and ephemeral arg stripping for dual RAG."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Kubernetes-style pod name tokens (suffix after workload hash) — conservative strip.
_RE_PODISH = re.compile(
    r"\b[a-z0-9][a-z0-9.-]*-[a-z0-9]{1,12}-[a-z0-9]{5}\b",
    re.IGNORECASE,
)
_RE_NS_DEPLOY = re.compile(
    r"\bnamespace\s*[:=]\s*([a-z0-9-]+)",
    re.IGNORECASE,
)
_RE_DEPLOY = re.compile(
    r"\bdeployment\s*[:=]\s*([a-z0-9-]+)",
    re.IGNORECASE,
)

_EPHEMERAL_ARG_KEYS = frozenset(
    {
        "pod_name",
        "pod",
        "pods",
        "instance",
        "node",
        "node_name",
        "hostname",
        "host",
        "container",
        "container_name",
        "uid",
    }
)


def strip_ephemeral_from_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Remove or redact volatile identifiers from tool args for stable playbook storage."""
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in args.items():
        lk = str(k).lower()
        if lk in _EPHEMERAL_ARG_KEYS:
            out[k] = "<ephemeral>"
        elif isinstance(v, dict):
            out[k] = strip_ephemeral_from_args(v)
        elif isinstance(v, list):
            out[k] = [
                strip_ephemeral_from_args(x) if isinstance(x, dict) else x for x in v[:64]
            ]
        else:
            out[k] = v
    return out


def canonical_symptom_text(text: str, *, strip_pods: bool = True) -> str:
    """Normalize whitespace/case; optionally replace pod-like tokens for cross-incident matching."""
    t = " ".join((text or "").strip().lower().split())[:4000]
    if strip_pods and t:
        t = _RE_PODISH.sub("<pod>", t)
    return t


def extract_workload_fingerprint(text: str) -> str:
    """Stable short fingerprint from namespace/deployment hints (best-effort)."""
    s = (text or "").lower()
    ns_m = _RE_NS_DEPLOY.search(s)
    dep_m = _RE_DEPLOY.search(s)
    ns = ns_m.group(1) if ns_m else ""
    dep = dep_m.group(1) if dep_m else ""
    raw = f"ns={ns}|dep={dep}"
    if raw == "ns=|dep=":
        h = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"hash:{h}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def stable_playbook_pattern_key(tool: str, symptom_text: str, args_playbook: dict[str, Any]) -> str:
    """Deterministic pattern key independent of ephemeral pod fields."""
    ah = hashlib.sha256(
        json.dumps(args_playbook or {}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:24]
    base = f"{tool}|{symptom_text[:500]}|{ah}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]
