"""Command channel — Omni enqueues diagnostic commands, agent polls and returns results.

Redis layout:
  omni:agent:cmd:{agent_id}                    LIST (LPUSH/RPOP) pending commands
  omni:diag:cmdresult:{cmd_id}                 STRING JSON command result
  omni:agent:profile:{agent_id}                STRING JSON VMProfile from discovery
  omni:remote_agent:registry:{agent_id}        STRING JSON agent registration (version field)

INVARIANT INV_READONLY_CMDS: gateway validates command is in COMMAND_WHITELIST
before enqueuing. Double enforcement — agent also validates on execution.
INVARIANT INV_NO_DATA_EXFIL: VMProfile accepts paths/names/stats only.
INVARIANT INV_HTTPS_ONLY: UPDATE_AGENT download_url must be https://.
INVARIANT INV_HOST_WHITELIST: download host validated against OMNI_AGENT_UPDATE_ALLOWED_HOSTS.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from gateway.tenant_context import get_tenant_ctx, is_admin_ctx, require_agent_tenant, resolve_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/agent", tags=["agent-commands"])

_CMD_QUEUE_PREFIX = "omni:agent:cmd:"
_CMD_RESULT_PREFIX = "omni:diag:cmdresult:"
_PROFILE_KEY_PREFIX = "omni:agent:profile:"
_REGISTRY_PREFIX = "omni:remote_agent:registry:"
# Expected release (version + bundle sha256), published by
# scripts/publish_agent_release.py — drift detection (Sprint NV-SRE IT-2).
_RELEASE_MANIFEST_KEY = "omni:agent:release_manifest"
# IT-5: release tarball (base64) do make publish-agent-release đẩy lên — agent
# tải qua kênh gateway đã xác thực (không URL ngoài, không SSRF).
_RELEASE_BUNDLE_KEY = "omni:agent:release_bundle"
_CMD_QUEUE_TTL = 300
_CMD_RESULT_TTL = 3600
_PROFILE_TTL = 86400
_UPDATE_CMD_TTL = 300
_MAX_QUEUE_DEPTH = 20
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")
_ALLOWED_HOSTS_ENV = "OMNI_AGENT_UPDATE_ALLOWED_HOSTS"


def _get_update_allowed_hosts() -> frozenset[str]:
    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "").strip()
    if not raw:
        return frozenset()
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _validate_update_url(url: str) -> tuple[bool, str]:
    allowed = _get_update_allowed_hosts()
    if not allowed:
        return False, f"{_ALLOWED_HOSTS_ENV} not configured"
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False, f"scheme_not_allowed: {parsed.scheme!r}"
        host = parsed.netloc.lower().split(":")[0]
        if not any(host == h or host.endswith("." + h) for h in allowed):
            return False, f"host_not_whitelisted: {host!r}"
    except Exception as exc:
        return False, f"url_parse_error: {exc}"
    return True, ""

# Must stay identical to remote_agent.command_executor.COMMAND_WHITELIST
# (the metadata-only set). Cannot import it directly — Dockerfile.gateway
# does not COPY src/remote_agent/. Any command here that is also in the
# agent's _CONTENT_READ_BLOCKED set would always be rejected agent-side
# anyway, so it must not appear here either (drift = misleading, not unsafe).
_COMMAND_WHITELIST = frozenset({
    "stat", "ls", "find", "du", "df",
    "ps", "pgrep", "top", "free", "vmstat", "iostat", "sar",
    "uptime", "uname", "id", "who", "last", "w",
    "ss", "netstat", "ip", "ping",
    "systemctl", "journalctl",
    "lsblk", "blkid",
    "mysqladmin",
    "dmesg", "lsof",
    "dpkg", "rpm",
    "file",
})


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


# ── Models ────────────────────────────────────────────────────────────────────

class CommandItem(BaseModel):
    command: str = Field(min_length=1, max_length=64)
    args: list[str] = Field(default_factory=list, max_length=20)
    timeout_s: int = Field(default=30, ge=1, le=120)
    trace_id: str = Field(default="", max_length=128)
    purpose: str = Field(default="", max_length=256)


class EnqueueCommandsRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    commands: list[CommandItem] = Field(max_length=10)


class CommandResultItem(BaseModel):
    cmd_id: str = Field(min_length=1, max_length=128)
    blocked: bool = False
    block_reason: str = Field(default="", max_length=256)
    stdout: str = Field(default="", max_length=8192)
    stderr: str = Field(default="", max_length=512)
    rc: int = 0
    duration_ms: int = 0


class AgentCommandResultRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    results: list[CommandResultItem] = Field(max_length=10)


class VMProfileRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=256)
    scanned_at: int = 0
    scan_duration_s: float = 0.0
    services: list[dict] = Field(default_factory=list, max_length=500)
    log_paths: list[str] = Field(default_factory=list, max_length=500)
    listeners: list[dict] = Field(default_factory=list, max_length=100)
    os_info: dict = Field(default_factory=dict)
    packages: list[dict] = Field(default_factory=list, max_length=500)


class UpdateAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    download_url: str = Field(min_length=8, max_length=2048)
    sha256_checksum: str = Field(min_length=32, max_length=128)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/commands/{agent_id}")
async def poll_commands(agent_id: str, request: Request) -> JSONResponse:
    """Agent polls to receive pending diagnostic commands from Omni."""
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise HTTPException(status_code=422, detail="Invalid agent_id")
    redis = _get_redis(request)
    await require_agent_tenant(redis, agent_id, get_tenant_ctx(request))
    queue_key = f"{_CMD_QUEUE_PREFIX}{agent_id}"

    commands: list[dict] = []
    for _ in range(10):
        raw = await redis.rpop(queue_key)
        if raw is None:
            break
        try:
            commands.append(json.loads(raw))
        except Exception:
            pass

    return JSONResponse(content={"commands": commands})


@router.post("/command-result")
async def receive_command_result(
    body: AgentCommandResultRequest, request: Request
) -> JSONResponse:
    """Agent POSTs command execution results. Stored for diagnosis loop to read."""
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    stored = 0
    for result in body.results:
        key = f"{_CMD_RESULT_PREFIX}{result.cmd_id}"
        value = json.dumps({
            "agent_id": body.agent_id,
            "cmd_id": result.cmd_id,
            "blocked": result.blocked,
            "block_reason": result.block_reason,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "rc": result.rc,
            "duration_ms": result.duration_ms,
            "received_at": int(time.time()),
        })
        await redis.set(key, value, ex=_CMD_RESULT_TTL)
        stored += 1
        logger.info(
            "[cmd-result] agent=%s cmd_id=%s rc=%d blocked=%s stdout_len=%d",
            body.agent_id, result.cmd_id, result.rc, result.blocked, len(result.stdout),
        )

    return JSONResponse(content={"stored": stored})


@router.post("/profile")
async def store_agent_profile(body: VMProfileRequest, request: Request) -> JSONResponse:
    """Agent POSTs VM discovery profile. Stored in Redis for analyst use."""
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    profile = body.model_dump()
    profile["stored_at"] = int(time.time())
    key = f"{_PROFILE_KEY_PREFIX}{body.agent_id}"
    await redis.set(key, json.dumps(profile), ex=_PROFILE_TTL)
    logger.info(
        "[agent-profile] stored agent_id=%s services=%d packages=%d listeners=%d",
        body.agent_id, len(body.services), len(body.packages), len(body.listeners),
    )
    return JSONResponse(content={"status": "stored", "agent_id": body.agent_id})


@router.post("/commands/enqueue")
async def enqueue_commands(body: EnqueueCommandsRequest, request: Request) -> JSONResponse:
    """Omni analyst enqueues diagnostic commands for agent to execute (whitelist enforced)."""
    redis = _get_redis(request)
    await require_agent_tenant(redis, body.agent_id, get_tenant_ctx(request))
    queue_key = f"{_CMD_QUEUE_PREFIX}{body.agent_id}"

    depth = await redis.llen(queue_key)
    if depth >= _MAX_QUEUE_DEPTH:
        raise HTTPException(status_code=429, detail="Command queue full for agent")

    enqueued: list[str] = []
    blocked: list[str] = []

    for cmd in body.commands:
        base = cmd.command.lstrip("/").split("/")[-1]
        if base not in _COMMAND_WHITELIST:
            blocked.append(cmd.command)
            logger.warning(
                "[cmd-enqueue] BLOCKED agent=%s cmd=%s reason=not_in_whitelist",
                body.agent_id, cmd.command,
            )
            continue
        cmd_id = f"cmd-{uuid.uuid4().hex[:12]}"
        payload = json.dumps({
            "cmd_id": cmd_id,
            "command": cmd.command,
            "args": cmd.args,
            "timeout_s": cmd.timeout_s,
            "trace_id": cmd.trace_id,
            "purpose": cmd.purpose,
            "enqueued_at": int(time.time()),
        })
        await redis.lpush(queue_key, payload)
        await redis.expire(queue_key, _CMD_QUEUE_TTL)
        enqueued.append(cmd_id)

    return JSONResponse(content={
        "enqueued": len(enqueued),
        "cmd_ids": enqueued,
        "blocked": blocked,
        "agent_id": body.agent_id,
    })


@router.post("/update")
async def enqueue_agent_update(body: UpdateAgentRequest, request: Request) -> JSONResponse:
    """Enqueue UPDATE_AGENT command for a specific agent.

    Gateway validates download_url against OMNI_AGENT_UPDATE_ALLOWED_HOSTS before enqueuing.
    Agent will download, verify sha256, backup, replace, and restart via systemctl.

    RCE-equivalent surface (fleet-wide remote code execution if abused) —
    restricted to admin keys regardless of tenant ownership of the agent_id.
    """
    if not is_admin_ctx(get_tenant_ctx(request)):
        raise HTTPException(status_code=403, detail="admin key required for agent updates")

    redis = _get_redis(request)

    ok, reason = _validate_update_url(body.download_url)
    if not ok:
        raise HTTPException(status_code=422, detail=f"update_url_blocked: {reason}")

    if not _AGENT_ID_RE.fullmatch(body.agent_id):
        raise HTTPException(status_code=422, detail="Invalid agent_id")

    cmd_id = f"upd-{uuid.uuid4().hex[:12]}"
    payload = json.dumps({
        "cmd_id": cmd_id,
        "type": "UPDATE_AGENT",
        "version": body.version,
        "download_url": body.download_url,
        "sha256_checksum": body.sha256_checksum,
        "enqueued_at": int(time.time()),
    })

    queue_key = f"{_CMD_QUEUE_PREFIX}{body.agent_id}"
    await redis.lpush(queue_key, payload)
    await redis.expire(queue_key, _UPDATE_CMD_TTL)

    logger.info(
        "[agent-update] enqueued cmd_id=%s agent=%s version=%s",
        cmd_id, body.agent_id, body.version,
    )
    return JSONResponse(content={
        "cmd_id": cmd_id,
        "agent_id": body.agent_id,
        "version": body.version,
        "status": "enqueued",
    })


def _classify_drift(rec: dict[str, Any], manifest: dict[str, Any] | None) -> str:
    """current | drifted | unknown — pure so tests can pin the contract.

    unknown = no published manifest, or the agent predates bundle-hash
    reporting; it must NEVER silently read as current.

    IT-4: agents running the AOIP employee runtime additionally report
    aoip_bundle_sha256; it must match the manifest's too. Legacy agents that
    don't report it are judged on bundle_sha256 alone (transition-safe). An
    agent reporting aoip against a manifest without it reads drifted — the
    running set differs from the published set; re-publish the manifest."""
    if not manifest or not manifest.get("bundle_sha256"):
        return "unknown"
    reported = str(rec.get("bundle_sha256") or "")
    if not reported:
        return "unknown"
    if reported != manifest.get("bundle_sha256") or rec.get("version") != manifest.get("version"):
        return "drifted"
    reported_aoip = str(rec.get("aoip_bundle_sha256") or "")
    if reported_aoip and reported_aoip != str(manifest.get("aoip_bundle_sha256") or ""):
        return "drifted"
    return "current"


async def _load_release_manifest(redis: Any) -> dict[str, Any] | None:
    raw = await redis.get(_RELEASE_MANIFEST_KEY)
    if not raw:
        return None
    try:
        manifest = json.loads(raw)
        return manifest if isinstance(manifest, dict) else None
    except Exception:
        return None


@router.get("/release/bundle")
async def download_release_bundle(request: Request) -> Response:
    """Agent tải release tarball (IT-5 safe update). Nguồn: Redis base64 do
    ``make publish-agent-release`` đẩy cùng lúc với manifest. Agent PHẢI verify
    sha256 vs ``release_tar_sha256`` trong command payload trước khi cài."""
    redis = _get_redis(request)
    raw = await redis.get(_RELEASE_BUNDLE_KEY)
    if not raw:
        raise HTTPException(status_code=404, detail="No release bundle published")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Corrupt release bundle: {exc}") from exc
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=omni-agent-release.tar.gz"})


@router.get("/versions")
async def list_agent_versions(request: Request, tenant_id: str | None = None) -> JSONResponse:
    """Return registered agents with version, bundle hash and drift status.

    Drift = agent's self-reported bundle sha256 / version differs from the
    published release manifest ("nhân viên chạy kiến thức cũ").

    Non-admin callers only see agents registered under their own tenant.
    Admin callers may narrow the list with ``?tenant_id=`` (same resolve_scope
    semantics as the onboarding routes).
    """
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, tenant_id)
    try:
        keys = await redis.keys(f"{_REGISTRY_PREFIX}*")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc

    manifest = await _load_release_manifest(redis)

    agents: list[dict] = []
    drifted_count = 0
    now = int(time.time())
    for key in sorted(keys):
        raw = await redis.get(key)
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if scope is not None and rec.get("tenant_id") != scope:
            continue
        age_s = now - int(rec.get("last_seen", 0))
        drift_status = _classify_drift(rec, manifest)
        if drift_status == "drifted":
            drifted_count += 1
            logger.warning(
                "[agent-drift] agent=%s version=%s bundle=%s… expected version=%s bundle=%s…",
                rec.get("agent_id"), rec.get("version"),
                str(rec.get("bundle_sha256") or "")[:12],
                manifest.get("version") if manifest else "?",
                str((manifest or {}).get("bundle_sha256") or "")[:12],
            )
        agents.append({
            "agent_id": rec.get("agent_id", ""),
            "tenant_id": rec.get("tenant_id", ""),
            "hostname": rec.get("hostname", ""),
            "version": rec.get("version", "unknown"),
            "bundle_sha256": rec.get("bundle_sha256", ""),
            "aoip_bundle_sha256": rec.get("aoip_bundle_sha256", ""),
            "drift_status": drift_status,
            "capabilities": rec.get("capabilities", []),
            "last_seen": rec.get("last_seen", 0),
            "age_seconds": age_s,
            "online": age_s < 120,
        })

    return JSONResponse(content={
        "agents": agents,
        "total": len(agents),
        "drifted": drifted_count,
        "release_manifest": manifest,
    })
