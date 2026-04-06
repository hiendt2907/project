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
    k = AsyncMock()
    out = await m.execute_shell(kafka=k, command="echo hi", session_id="s", trace_id="t")
    assert "tắt" in out.lower() or "disabled" in out.lower()
    k.send_dict.assert_called()
    assert k.send_dict.call_args[0][0] == ws.kafka_topic_audit_sandbox


@pytest.mark.asyncio
async def test_policy_deny_rm_rf_audits() -> None:
    ws = WorkerSettings(opensandbox_enabled=True)
    m = SandboxManager(ws)
    k = AsyncMock()
    out = await m.execute_shell(kafka=k, command="rm -rf /tmp/x", session_id="s", trace_id="tr-1")
    assert "policy" in out.lower() or "từ chối" in out.lower()
    assert k.send_dict.call_count >= 1
    assert k.send_dict.call_args[0][0] == ws.kafka_topic_audit_sandbox
