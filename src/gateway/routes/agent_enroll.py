"""POST /webhook/agent/enroll — đổi one-time enroll token lấy credential per-agent (IT-3).

Router này KHÔNG gắn guard _require_api_key: enroll token trong body chính là
credential (một lần). Mọi validate/single-use đều nằm trong một transaction PG
(AdminConfigRepo.consume_enroll_token_and_issue_credential) — request thứ hai
cùng token, kể cả race song song, nhận 401.

Plaintext per-agent key chỉ trả về đúng MỘT lần trong response enroll; PG chỉ
lưu sha256. Credential rotation là non-goal của sprint (ghi risk register).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/agent", tags=["remote-agent-enroll"])

# Chống brute-force token: giới hạn enroll theo IP nguồn (Redis INCR cửa sổ 60s).
_ENROLL_RL_PREFIX = "omni:enrollrl:"
_ENROLL_RL_WINDOW_S = 60
_ENROLL_RL_LIMIT = 10


class AgentEnrollRequest(BaseModel):
    enroll_token: str = Field(min_length=16, max_length=256)
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    hostname: str = Field(default="", max_length=256)


def _get_admin_repo(request: Request) -> Any:
    repo = getattr(request.app.state, "admin_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Admin config store not available")
    return repo


async def _enforce_enroll_rate_limit(request: Request) -> None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:  # enroll vẫn hoạt động khi Redis down — PG mới là gate thật
        return
    client_ip = request.client.host if request.client else "unknown"
    key = f"{_ENROLL_RL_PREFIX}{client_ip}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _ENROLL_RL_WINDOW_S)
        if count > _ENROLL_RL_LIMIT:
            raise HTTPException(status_code=429, detail="Too many enroll attempts")
    except HTTPException:
        raise
    except Exception:  # Redis lỗi → không chặn enroll hợp lệ
        logger.warning("[AGENT-ENROLL] rate-limit check failed (Redis)", exc_info=True)


@router.post("/enroll")
async def enroll_agent(body: AgentEnrollRequest, request: Request) -> JSONResponse:
    """Đổi enroll token (one-time) lấy per-agent API key. Trả plaintext MỘT lần."""
    await _enforce_enroll_rate_limit(request)
    repo = _get_admin_repo(request)

    token_hash = hashlib.sha256(body.enroll_token.encode()).hexdigest()
    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    result = await repo.consume_enroll_token_and_issue_credential(
        token_hash=token_hash,
        agent_id=body.agent_id,
        hostname=body.hostname,
        key_hash=key_hash,
        key_prefix=raw_key[:8],
    )
    if result is None:
        logger.warning(
            "[AGENT-ENROLL] rejected agent_id=%s (token invalid/used/expired)", body.agent_id,
        )
        raise HTTPException(status_code=401, detail="Enroll token invalid, already used, or expired")

    logger.info(
        "[AGENT-ENROLL] issued credential tenant=%s agent=%s key_prefix=%s",
        result["tenant_id"], body.agent_id, result["key_prefix"],
    )
    return JSONResponse(
        status_code=201,
        content={
            "status": "enrolled",
            "tenant_id": result["tenant_id"],
            "agent_id": body.agent_id,
            # plaintext CHỈ trả lần này — PG lưu sha256, không hiển thị lại được.
            "api_key": raw_key,
            "key_prefix": result["key_prefix"],
        },
    )
