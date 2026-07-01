"""Test: long-running daemon resume + bounded tick loop (Step 6 smoke, không cần VM).

Chứng minh daemon RESUME command dang dở trước khi tick, và tick xử lý command mới. Proof
process-restart/reboot thật ở scripts/prove_durable_delivery trên VM systemd.
"""
from __future__ import annotations

from aoip.agent.daemon import run_daemon
from aoip.agent.inbox import L_OUTCOME_RECORDED, LocalInbox

AGENT, TENANT = "ubuntu-edge-1", "acme"


class FakeClient:
    def __init__(self, commands):
        self._commands = commands
        self.terminal_calls = 0

    async def poll_runtime(self, agent_id):
        out, self._commands = self._commands, []
        return out

    async def accept(self, *a, **k):
        pass

    async def progress(self, *a, **k):
        pass

    async def report_terminal(self, agent_id, tenant_id, command_id, state, outcome, **k):
        self.terminal_calls += 1
        return {"acknowledged": True, "state": state}


async def test_daemon_resumes_then_ticks(tmp_path):
    box = LocalInbox(str(tmp_path))
    # command dang dở từ "lần chạy trước": outcome đã ghi, chưa report (crash trước ack)
    box.persist("cmd-old", tenant_id=TENANT, payload={})
    box.record_outcome("cmd-old", {"rc": 0})
    assert box.get("cmd-old").local_state == L_OUTCOME_RECORDED

    client = FakeClient([{"command_id": "cmd-new", "tenant_id": TENANT, "payload": {}}])

    async def executor(payload):
        return "COMPLETED", {"rc": 0}

    await run_daemon(agent_id=AGENT, tenant=TENANT, gateway="http://x", api_key="",
                     inbox_root=str(tmp_path), interval_s=0, executor=executor,
                     max_ticks=1, client=client)

    # cmd-old được re-report khi resume; cmd-new xử lý trong tick → cả hai archived
    assert client.terminal_calls == 2
    assert box.pending() == []
