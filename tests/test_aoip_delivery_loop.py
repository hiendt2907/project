"""Tests: agent DeliveryLoop — execute-once, crash-resume, duplicate delivery, Gateway outage.

Executor được đếm số lần gọi → chứng minh mutation chạy ĐÚNG MỘT LẦN qua duplicate delivery
và agent restart. Client giả mô phỏng terminal-ack + outage.
"""
from __future__ import annotations

import pytest

from aoip.agent.delivery_loop import DeliveryLoop
from aoip.agent.inbox import L_OUTCOME_RECORDED, L_RUNNING, LocalInbox

AGENT, TENANT = "agent-1", "acme"


class FakeClient:
    def __init__(self, commands, *, gateway_up=True):
        self._commands = commands
        self.gateway_up = gateway_up
        self.terminal_calls = 0
        self.accepted = []

    async def poll_runtime(self, agent_id):
        out, self._commands = self._commands, []
        return out

    async def accept(self, agent_id, tenant_id, command_id, **k):
        self.accepted.append(command_id)

    async def progress(self, agent_id, tenant_id, command_id, phase, **k):
        pass

    async def report_terminal(self, agent_id, tenant_id, command_id, state, outcome, **k):
        self.terminal_calls += 1
        if not self.gateway_up:
            raise ConnectionError("gateway down")
        return {"acknowledged": True, "state": state}


def _cmd(cid="cmd-1"):
    return {"command_id": cid, "tenant_id": TENANT, "payload": {"verb": "restart"}}


async def test_tick_executes_once_and_archives(tmp_path):
    box = LocalInbox(str(tmp_path))
    calls = []

    async def executor(payload):
        calls.append(payload)
        return "COMPLETED", {"rc": 0}

    client = FakeClient([_cmd()])
    loop = DeliveryLoop(agent_id=AGENT, client=client, inbox=box, executor=executor)
    assert await loop.tick() == 1
    assert len(calls) == 1 and client.terminal_calls == 1
    assert box.pending() == []                       # archived sau terminal ack


async def test_preack_redelivery_executes_once(tmp_path):
    """Redelivery TRƯỚC terminal-ack (Gateway chưa nhận terminal): re-report, KHÔNG re-mutate.

    Post-ack thì Gateway KHÔNG giao lại (record terminal — xem test_aoip_delivery), nên
    duplicate mà agent thực sự thấy là pre-ack. Local inbox giữ outcome → chỉ re-report.
    """
    box = LocalInbox(str(tmp_path))
    calls = []

    async def executor(payload):
        calls.append(payload)
        return "COMPLETED", {"rc": 0}

    # tick 1: executor chạy, nhưng report_terminal fail (Gateway chưa ack) → entry còn lại
    down = FakeClient([_cmd()], gateway_up=False)
    with pytest.raises(ConnectionError):
        await DeliveryLoop(agent_id=AGENT, client=down, inbox=box, executor=executor).tick()

    # tick 2: Gateway giao lại cùng command_id (chưa terminal) → has_outcome → re-report
    up = FakeClient([_cmd()], gateway_up=True)
    await DeliveryLoop(agent_id=AGENT, client=up, inbox=box, executor=executor).tick()
    assert len(calls) == 1                             # mutation ĐÚNG 1 lần
    assert up.terminal_calls == 1 and box.pending() == []


async def test_gateway_outage_preserves_outcome_then_reports_on_resume(tmp_path):
    box = LocalInbox(str(tmp_path))

    async def executor(payload):
        return "COMPLETED", {"rc": 0}

    down = FakeClient([_cmd()], gateway_up=False)
    with pytest.raises(ConnectionError):
        await DeliveryLoop(agent_id=AGENT, client=down, inbox=box, executor=executor).tick()
    # outcome đã bền cục bộ dù report fail
    assert box.get("cmd-1").local_state == L_OUTCOME_RECORDED

    # Gateway lên lại → resume re-report, KHÔNG execute lại
    calls = []

    async def executor2(payload):
        calls.append(payload)
        return "COMPLETED", {"rc": 0}

    up = FakeClient([], gateway_up=True)
    handled = await DeliveryLoop(agent_id=AGENT, client=up, inbox=box, executor=executor2).resume()
    assert handled == 1 and calls == []               # re-report only
    assert box.pending() == []


async def test_resume_running_without_outcome_reconciles(tmp_path):
    box = LocalInbox(str(tmp_path))
    box.persist("cmd-1", tenant_id=TENANT, payload={"verb": "restart"})
    box.set_state("cmd-1", L_RUNNING)                 # crash giữa RUNNING, chưa outcome

    reconciled = []

    async def reconciler(entry):
        reconciled.append(entry.command_id)
        return "COMPLETED", {"rc": 0, "reconciled": True}

    async def executor(payload):
        raise AssertionError("executor không được gọi khi reconcile")

    up = FakeClient([], gateway_up=True)
    loop = DeliveryLoop(agent_id=AGENT, client=up, inbox=box, executor=executor,
                        reconciler=reconciler)
    assert await loop.resume() == 1
    assert reconciled == ["cmd-1"]                    # reconcile, KHÔNG blind re-execute
    assert box.pending() == []
