"""Remote Agent entrypoint — vòng đời đầy đủ trên host THẬT (EPIC 2).

    python -m aoip.agent.main --tenant acme [--ssh user@host] [--lab-orb vm]

Mặc định chạy LocalTransport (agent đã cài trên chính host này). Có thể trỏ tới
host từ xa qua SSH, hoặc dùng OrbStack chỉ trong lab. Cùng một code — chỉ transport
đổi → bằng chứng môi trường không phải dependency kiến trúc.

Vòng: Identity → Register → Heartbeat → Mission Pull → Observe (understand_host) →
Report (AgentStatus dashboard).
"""
from __future__ import annotations

import argparse
import asyncio

from aoip.agent.runtime import RemoteAgent
from aoip.capabilities.missions import understand_host_mission
from aoip.capability import CapabilityState
from aoip.evidence import EvidenceCompletionEngine, InferenceResolver, RuntimeResolver
from aoip.omni.control_plane import Omni
from aoip.remote_linux_backend import RemoteLinuxBackend
from aoip.system_model import SystemModel
from aoip.transport import LocalTransport, OrbTransport, SSHTransport
from aoip.understanding import UnderstandingContext


def _build_transport(args):
    if args.lab_orb:
        return OrbTransport(args.lab_orb)          # LAB only
    if args.ssh:
        user, _, host = args.ssh.partition("@")
        return SSHTransport(host or user, user=user if host else None)
    return LocalTransport(target=args.host or "localhost")


async def _run_mission(agent: RemoteAgent, goal: str):
    host = agent.identity.host
    ctx = UnderstandingContext(
        host=host,
        scope=f"{agent.identity.tenant}/{host}",
        backend=RemoteLinuxBackend(agent.transport),
        capability=CapabilityState(capability_id=goal, scope=f"{agent.identity.tenant}/{host}"),
        model=SystemModel(scope=f"{agent.identity.tenant}/{host}"),
    )
    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=lambda n: None),  # chưa có tenant registry → để hở thành câu hỏi
    ])
    mission = await understand_host_mission(ctx, engine=engine)
    return mission, ctx


async def run(args) -> None:
    omni = Omni()  # control plane (in-process bootstrap)
    transport = _build_transport(args)
    agent = RemoteAgent(transport=transport, tenant=args.tenant, omni=omni)

    print(f"[bootstrap] identity = {agent.identity.agent_id} host={agent.identity.host}")
    await agent.register()
    await agent.heartbeat()
    print("[bootstrap] registered + heartbeat sent")

    # Omni cấp mission đầu tiên cho host mới (Day-1: hiểu host).
    omni.assign_mission(agent.identity.agent_id, goal="understand_host")
    goal = await agent.pull_mission()
    print(f"[mission] pulled: {goal}")

    mission, ctx = await _run_mission(agent, goal)
    coverage = mission.completion
    questions = len(ctx.communications)
    next_mission = "understand_network" if questions == 0 else "answer_questions"

    status = agent.status(
        knowledge_coverage=coverage,
        questions_outstanding=questions,
        capability_k=ctx.capability.dimensions.get("K", 0.0),
        next_mission=next_mission,
    )
    print("\n=== AGENT STATUS (dashboard khách hàng) ===")
    print(status.render())
    if ctx.communications:
        print("\n  Outstanding questions:")
        for c in ctx.communications:
            print(f"   ❓ {c.question}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", default="acme")
    p.add_argument("--host", default=None, help="nhãn host cho LocalTransport")
    p.add_argument("--ssh", default=None, help="user@host chạy qua SSH")
    p.add_argument("--lab-orb", default=None, help="tên VM OrbStack (LAB ONLY)")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
