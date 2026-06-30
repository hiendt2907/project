"""Live runtime — chạy Mission understand_host trên MÁY THẬT đang chạy (EPIC 1).

    python -m aoip.live

KHÔNG mock, KHÔNG profile bịa. Đọc tiến trình thật + probe cổng thật của host này,
chạy qua đúng Mission Runtime đã có, in ra SystemModel thật + Mission Completion.
Đây là bước đầu của North Star: agent chạm hạ tầng thật và tự hiểu nó.
"""
from __future__ import annotations

import asyncio
import socket

from aoip.capabilities.missions import understand_host_mission
from aoip.capability import CapabilityState
from aoip.evidence import EvidenceCompletionEngine, InferenceResolver, RuntimeResolver
from aoip.live_backend import LiveHostDiscoveryBackend
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


async def _real_prober(node: str) -> str | None:
    """Evidence runtime THẬT: thử phân giải DNS + probe cổng quy ước của node.

    node dạng 'svc:redis' / 'db:postgres'. Tách tên, thử DNS resolve; nếu được →
    coi như định vị được (không hỏi người). KHÔNG bịa: thất bại → trả None.
    """
    name = node.split(":", 1)[-1]
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        if infos:
            return infos[0][4][0]
    except (socket.gaierror, OSError):
        return None
    return None


async def run_live() -> None:
    host = socket.gethostname()
    ctx = UnderstandingContext(
        host=host,
        scope=f"local/{host}",
        backend=LiveHostDiscoveryBackend(),
        capability=CapabilityState(capability_id="understand_host", scope=f"local/{host}"),
        model=SystemModel(scope=f"local/{host}"),
    )
    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=_real_prober),  # DNS/socket thật
    ])

    print(f"=== AOIP LIVE — understand_host trên máy THẬT: {host} ===")
    mission = await understand_host_mission(ctx, engine=engine)

    for line in ctx.trace:
        print("  " + line)
    print("\n  SystemModel (Fact THẬT từ máy này):")
    for f in ctx.model.facts:
        print(f"    {f.subject} --{f.predicate}--> {f.obj}  (conf={f.confidence})")
    if not ctx.model.facts:
        print("    (không phát hiện service nào trên cổng quy ước)")
    for c in ctx.communications:
        print(f"  ❓ INTERVIEW (đã exhaust evidence): {c.question}")

    print(
        f"\n  MISSION: {mission.goal} → {mission.state.value} "
        f"completion={mission.completion:.0%}  DoD✗={list(mission.dod_failed)}"
    )
    print(f"  KPI khách hàng thấy: 'AI hiểu host {host} {mission.completion:.0%}'")


if __name__ == "__main__":
    asyncio.run(run_live())
