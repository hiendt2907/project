"""Execute backends + approval gate cho walking skeleton.

Backend là seam thực thi: skeleton dùng ``MockK8sBackend`` (chạy không cần
cluster). Backend thật (kubernetes_asyncio) wire sau — runtime sẽ ép, không phải
suy luận trước (lộ trình CTO: Implementation → Validation → Architecture).
"""
from __future__ import annotations

from typing import Protocol


class K8sBackend(Protocol):
    async def rollout_restart(self, namespace: str, name: str) -> dict: ...
    async def rollout_status(self, namespace: str, name: str) -> dict: ...


class MockK8sBackend:
    """Deterministic backend cho skeleton/test. ``fail_restart`` để test nhánh Recover."""

    def __init__(self, *, fail_restart: bool = False) -> None:
        self._fail = fail_restart
        self._restarted: set[str] = set()

    async def rollout_restart(self, namespace: str, name: str) -> dict:
        key = f"{namespace}/{name}"
        if self._fail:
            return {"ok": False, "error": "simulated rollout failure", "target": key}
        self._restarted.add(key)
        return {"ok": True, "target": key}

    async def rollout_status(self, namespace: str, name: str) -> dict:
        key = f"{namespace}/{name}"
        # Sau restart thành công: pod ready, age reset → unhealthy=False.
        healthy = key in self._restarted
        return {"target": key, "ready": healthy, "unhealthy_pods": 0 if healthy else 1}


class ApprovalGate(Protocol):
    async def approve(self, scope: str, plan: str) -> bool: ...


class AutoApprove:
    """Skeleton-only. Thật = HITL/Governance (Approve KHÔNG phải Execution primitive)."""

    async def approve(self, scope: str, plan: str) -> bool:  # noqa: D401
        return True
