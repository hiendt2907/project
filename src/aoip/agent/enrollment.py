"""Enrollment client — "tuyển dụng" chính thức một Remote Agent (IT-3, ADR-001).

Đổi one-time enroll token (do Omni Admin API phát) lấy credential per-agent qua
``POST /webhook/agent/enroll``. Plaintext key chỉ tồn tại trong response đúng một
lần — caller (installer / daemon bootstrap) chịu trách nhiệm persist an toàn
(run.env chmod 600 trên VM khách hàng). Module thuần logic + httpx, không đọc
env, không side-effect ngoài HTTP call — dễ test và tái dùng từ cả installer
lẫn ``aoip.agent.daemon`` sau này.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

_ENROLL_PATH = "/webhook/agent/enroll"
_TIMEOUT_S = 15.0


class EnrollmentError(RuntimeError):
    """Enroll bị từ chối (token sai/đã dùng/hết hạn) hoặc gateway lỗi."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"enroll failed: HTTP {status_code} — {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class EnrollmentResult:
    tenant_id: str
    agent_id: str
    api_key: str      # plaintext — chỉ có ở đây, persist ngay rồi bỏ
    key_prefix: str


async def enroll_agent(
    gateway_url: str,
    *,
    enroll_token: str,
    agent_id: str,
    hostname: str = "",
    client: httpx.AsyncClient | None = None,
) -> EnrollmentResult:
    """Đổi enroll token lấy per-agent credential. Raise EnrollmentError khi bị
    từ chối — token là one-time nên caller KHÔNG retry mù (token thứ hai phải
    do admin phát lại)."""
    payload: dict[str, Any] = {
        "enroll_token": enroll_token,
        "agent_id": agent_id,
        "hostname": hostname,
    }
    url = gateway_url.rstrip("/") + _ENROLL_PATH
    if client is not None:
        resp = await client.post(url, json=payload, timeout=_TIMEOUT_S)
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as own_client:
            resp = await own_client.post(url, json=payload)
    if resp.status_code != 201:
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))
        except Exception:
            detail = resp.text[:200]
        raise EnrollmentError(resp.status_code, detail)
    data = resp.json()
    return EnrollmentResult(
        tenant_id=data["tenant_id"],
        agent_id=data["agent_id"],
        api_key=data["api_key"],
        key_prefix=data["key_prefix"],
    )
