"""Tests EPIC 2: Remote Agent bootstrap lifecycle + Omni control plane.

Product runtime thật (không chỉ discovery): Install→Identity→Register→Heartbeat→
Mission Pull→Observe→Report. Omni = control plane giữ registry + mission queue +
capability per agent. KPI = AgentStatus dashboard (installed/registered/heartbeat/
mission/knowledge coverage/questions/Capability(K)/next mission).
"""
from __future__ import annotations

import pytest

from aoip.agent.identity import AgentIdentity, derive_identity
from aoip.agent.runtime import RemoteAgent
from aoip.omni.control_plane import Omni
from aoip.transport import LocalTransport


def test_identity_is_stable_and_derived_from_host():
    t = LocalTransport(target="ec2-prod-1")
    id1 = derive_identity(t, tenant="acme")
    id2 = derive_identity(t, tenant="acme")
    assert isinstance(id1, AgentIdentity)
    assert id1.agent_id == id2.agent_id  # ổn định theo host+tenant
    assert id1.tenant == "acme"
    assert id1.host == "ec2-prod-1"


async def test_register_then_heartbeat_tracked_by_omni():
    omni = Omni()
    agent = RemoteAgent(transport=LocalTransport(target="h1"), tenant="acme", omni=omni)

    await agent.register()
    assert omni.is_registered(agent.identity.agent_id)

    await agent.heartbeat()
    rec = omni.agent_record(agent.identity.agent_id)
    assert rec["heartbeats"] == 1
    assert rec["state"] == "online"


async def test_omni_assigns_mission_and_agent_pulls_it():
    omni = Omni()
    agent = RemoteAgent(transport=LocalTransport(target="h1"), tenant="acme", omni=omni)
    await agent.register()

    omni.assign_mission(agent.identity.agent_id, goal="understand_host")
    pulled = await agent.pull_mission()
    assert pulled == "understand_host"
    # queue rỗng sau khi pull.
    assert await agent.pull_mission() is None


async def test_agent_status_is_customer_facing_kpi():
    omni = Omni()
    agent = RemoteAgent(transport=LocalTransport(target="h1"), tenant="acme", omni=omni)
    await agent.register()
    await agent.heartbeat()

    status = agent.status(knowledge_coverage=0.83, questions_outstanding=2, capability_k=0.78,
                          next_mission="understand_network")
    assert status.installed and status.registered
    assert status.heartbeats == 1
    assert status.knowledge_coverage == pytest.approx(0.83)
    assert status.questions_outstanding == 2
    assert status.capability_k == pytest.approx(0.78)
    assert status.next_mission == "understand_network"
    # render được thành dashboard text.
    text = status.render()
    assert "83%" in text and "understand_network" in text
