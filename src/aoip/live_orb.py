"""Live tenant onboarding — Mission understand_tenant trên VM Linux THẬT (EPIC 1+3).

    python -m aoip.live_orb [vm1 vm2 ...]   (mặc định: cust-edge cust-app cust-db)

KHÔNG mock. Mỗi VM là một host thật; runtime chạy understand_host Mission trên
từng VM (discovery thật qua orb), rồi gộp thành understand_tenant với Mission
Completion thật. Đây là North Star ở quy mô nhỏ: agent tiếp nhận một hệ thống
nhiều máy và tự báo "đã hiểu đến đâu".
"""
from __future__ import annotations

import asyncio
import sys

from aoip.capabilities.missions import understand_host_mission
from aoip.capability import CapabilityState
from aoip.evidence import EvidenceCompletionEngine, InferenceResolver, RuntimeResolver
from aoip.mission import Mission, MissionState, aggregate_completion
from aoip.orb_backend import OrbVMDiscoveryBackend
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext

DEFAULT_VMS = ["cust-edge", "cust-app", "cust-db"]


def _make_prober(vms: list[str]):
    """Evidence runtime THẬT cấp tenant: một node định vị được nếu CÓ VM tên trùng.

    Mô phỏng "search other host" — postgres/cust-db xuất hiện ở host khác → giải
    được, không hỏi người. KHÔNG bịa: tên lạ → None → câu hỏi kiến trúc.
    """
    known = {v.lower() for v in vms}

    async def prober(node: str) -> str | None:
        name = node.split(":", 1)[-1].lower()
        return f"vm:{name}" if name in known else None

    return prober


async def _understand_vm(vm: str, prober) -> tuple[Mission, UnderstandingContext]:
    ctx = UnderstandingContext(
        host=vm,
        scope=f"acme/{vm}",
        backend=OrbVMDiscoveryBackend(vm),
        capability=CapabilityState(capability_id="understand_host", scope=f"acme/{vm}"),
        model=SystemModel(scope=f"acme/{vm}"),
    )
    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=prober),
    ])
    mission = await understand_host_mission(
        ctx, engine=engine, parent_mission_id="understand_tenant:acme"
    )
    return mission, ctx


async def run(vms: list[str]) -> None:
    prober = _make_prober(vms)
    print(f"=== AOIP LIVE — understand_tenant trên VM THẬT: {vms} ===\n")

    results = await asyncio.gather(*(_understand_vm(vm, prober) for vm in vms))

    subs: list[Mission] = []
    for (mission, ctx), vm in zip(results, vms):
        subs.append(mission)
        print(f"── {vm} ──  Mission {mission.state.value}  completion={mission.completion:.0%}")
        for f in ctx.model.facts:
            print(f"     {f.subject} --{f.predicate}--> {f.obj}")
        for t in sorted(ctx.model.unknown_edge_targets):
            print(f"     ⚠️ unknown node: {t}")
        for c in ctx.communications:
            print(f"     ❓ {c.question}")
        print()

    completion = aggregate_completion(subs)
    all_done = all(s.state is MissionState.COMPLETED for s in subs)
    state = MissionState.COMPLETED if all_done else MissionState.BLOCKED
    print(f"=== TENANT acme → {state.value}  Mission Completion = {completion:.0%} ===")
    print(f"KPI khách hàng thấy: 'AI đã hiểu hệ thống acme {completion:.0%}'")


if __name__ == "__main__":
    targets = sys.argv[1:] or DEFAULT_VMS
    asyncio.run(run(targets))
