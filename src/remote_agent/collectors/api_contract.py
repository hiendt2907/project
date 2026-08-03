"""Read OpenAPI/Swagger contracts locally and emit route metadata only."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pkg.domain.taxonomy import APPLICATION
from remote_agent.evidence import build_envelope

try:
    import yaml
except ImportError:  # pragma: no cover - minimal agent installs may be JSON-only
    yaml = None

_MAX_BYTES = 512_000
_MAX_ROUTES = 500
_CANDIDATE_NAMES = ("openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml", "swagger.yml")
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _base_path(document: dict[str, Any]) -> str:
    if document.get("basePath"):
        return str(document["basePath"])[:120]
    servers = document.get("servers") or []
    if servers and isinstance(servers[0], dict):
        return urlparse(str(servers[0].get("url") or "")).path[:120]
    return ""


def _load_document(content: str, path: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content) if path.lower().endswith(".json") else (yaml.safe_load(content) if yaml else None)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_api_contract(content: str, path: str) -> dict[str, Any] | None:
    """Extract a bounded, secret-free contract summary from OpenAPI v2/v3."""
    document = _load_document(content, path)
    if not document or not (document.get("openapi") or document.get("swagger")):
        return None
    routes: list[dict[str, Any]] = []
    for route, path_item in (document.get("paths") or {}).items():
        if not isinstance(route, str) or not route.startswith("/") or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _METHODS or not isinstance(operation, dict):
                continue
            responses = operation.get("responses") or {}
            routes.append({
                "method": method.upper(),
                "route": route[:200],
                "operation_id": str(operation.get("operationId") or "")[:120],
                "tags": [str(tag)[:80] for tag in (operation.get("tags") or [])[:10]],
                "response_statuses": [str(status)[:20] for status in responses.keys()][:20],
            })
            if len(routes) >= _MAX_ROUTES:
                break
        if len(routes) >= _MAX_ROUTES:
            break
    if not routes:
        return None
    return {
        "path": path[:200],
        "format": "openapi" if document.get("openapi") else "swagger",
        "version": str(document.get("openapi") or document.get("swagger"))[:30],
        "title": str((document.get("info") or {}).get("title") or "")[:160],
        "base_path": _base_path(document),
        "routes": routes,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _candidate_paths(search_dirs: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw_dir in search_dirs:
        base = Path(raw_dir)
        if not base.is_dir():
            continue
        for name in _CANDIDATE_NAMES:
            path = base / name
            if path.is_file() and not path.is_symlink():
                found.append(path)
        # Common application layouts: /app/docs/openapi.json, /srv/api/swagger.yaml.
        for name in _CANDIDATE_NAMES:
            for path in base.glob(f"*/{name}"):
                if path.is_file() and not path.is_symlink():
                    found.append(path)
    return list(dict.fromkeys(found))


async def collect_api_contracts(hostname: str, search_dirs: list[str]) -> dict[str, Any] | None:
    contracts: list[dict[str, Any]] = []
    for path in _candidate_paths(search_dirs):
        try:
            content = await asyncio.get_event_loop().run_in_executor(None, lambda p=path: p.read_text(errors="replace")[:_MAX_BYTES])
            contract = parse_api_contract(content, str(path))
            if contract:
                contracts.append(contract)
        except Exception:
            continue
    if not contracts:
        return None
    return build_envelope(
        probe="api_contract",
        lane="APP_HTTP",
        domain=APPLICATION,
        result="PASSED",
        extracted_fact={"discovery_data": {"api_contracts": contracts[:20]}},
        alert_rule="RemoteApiContractObserved",
        alert_hint=f"[{hostname}] observed {len(contracts)} OpenAPI/Swagger contract(s)",
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )
