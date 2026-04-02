"""OpenSandbox manager — disabled path + unified audit stream."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from execution.manager import SandboxManager
from workers.settings import WorkerSettings


@pytest.mark.asyncio
async def test_execute_disabled_audits() -> None:
    ws = WorkerSettings(opensandbox_enabled=False)
    m = SandboxManager(ws)
    r = AsyncMock()
    out = await m.execute_shell(redis=r, command="echo hi", session_id="s", trace_id="t")
    assert "tắt" in out.lower() or "disabled" in out.lower()
    r.xadd.assert_called()
    assert r.xadd.call_args[0][0] == ws.audit_sandbox_stream


@pytest.mark.asyncio
async def test_policy_deny_rm_rf_audits() -> None:
    ws = WorkerSettings(opensandbox_enabled=True)
    m = SandboxManager(ws)
    r = AsyncMock()
    out = await m.execute_shell(redis=r, command="rm -rf /tmp/x", session_id="s", trace_id="tr-1")
    assert "policy" in out.lower() or "từ chối" in out.lower()
    assert r.xadd.call_count >= 1
    assert r.xadd.call_args[0][0] == ws.audit_sandbox_stream
