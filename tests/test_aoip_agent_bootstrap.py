"""Tests EPIC 2: Remote Agent bootstrap qua OmniClient interface.

AOIP KHÔNG sở hữu Control Plane. Agent chỉ thấy ``OmniClient`` (register/heartbeat/
fetch_missions/submit_result/submit_evidence). Test dùng InProcessOmniClient (bọc
stub Omni) — KHÔNG mock HTTP; round-trip gateway THẬT ở test_aoip_omni_http_e2e.
"""
from __future__ import annotations

import pytest

from aoip.agent.identity import AgentIdentity, derive_identity
from aoip.agent.omni_client import InProcessOmniClient
from aoip.agent.runtime import RemoteAgent
from aoip.omni.control_plane import Omni
from aoip.transport import LocalTransport


def _agent(omni: Omni) -> RemoteAgent:
    return RemoteAgent(transport=LocalTransport(target="h1"), tenant="acme",
                       omni=InProcessOmniClient(omni))


def test_identity_is_stable_and_derived_from_host():
    t = LocalTransport(target="ec2-prod-1")
    id1 = derive_identity(t, tenant="acme")
    id2 = derive_identity(t, tenant="acme")
    assert isinstance(id1, AgentIdentity)
    assert id1.agent_id == id2.agent_id
    assert id1.tenant == "acme" and id1.host == "ec2-prod-1"


async def test_register_then_heartbeat_tracked():
    omni = Omni()
    agent = _agent(omni)

    await agent.register()
    assert omni.is_registered(agent.identity.agent_id)

    await agent.heartbeat()
    assert agent.status().heartbeats == 1
    assert omni.agent_record(agent.identity.agent_id)["state"] == "online"


async def test_omni_assigns_mission_and_agent_pulls_it():
    omni = Omni()
    agent = _agent(omni)
    await agent.register()

    omni.assign_mission(agent.identity.agent_id, goal="understand_host")
    assert await agent.pull_mission() == "understand_host"
    assert await agent.pull_mission() is None  # queue rỗng


async def test_agent_reports_result_and_evidence_through_client():
    omni = Omni()
    agent = _agent(omni)
    await agent.register()
    omni.assign_mission(agent.identity.agent_id, goal="understand_host")
    await agent.pull_mission()

    await agent.report_result(rc=0, stdout="done")
    await agent.report_evidence([{"probe": "discovery", "result": "PASSED"}])
    assert omni.results and omni.results[0]["rc"] == 0
    assert omni.evidence and omni.evidence[0]["items"][0]["probe"] == "discovery"


async def test_agent_status_is_customer_facing_kpi():
    omni = Omni()
    agent = _agent(omni)
    await agent.register()
    await agent.heartbeat()

    status = agent.status(knowledge_coverage=0.83, questions_outstanding=2,
                          capability_k=0.78, next_mission="understand_network")
    assert status.installed and status.registered and status.heartbeats == 1
    assert status.knowledge_coverage == pytest.approx(0.83)
    text = status.render()
    assert "83%" in text and "understand_network" in text
