"""Walking-skeleton runner — chạy capability end-to-end, in vòng phản hồi Capability.

    python -m aoip.runner

Chứng minh: pipeline chạy thật; sau mỗi lần thành công, CapabilityState (chiều E +
maturity) tăng — vòng Observe→…→Assess→Capability update đã đóng.
"""
from __future__ import annotations

import asyncio

from aoip.backends import AutoApprove, MockK8sBackend
from aoip.capabilities.restart_deployment import restart_deployment
from aoip.capability import CapabilityState
from aoip.context import ExecutionContext


async def run_once(capability_state: CapabilityState, *, fail: bool = False) -> ExecutionContext:
    ctx = ExecutionContext(
        scope="payment/web",
        backend=MockK8sBackend(fail_restart=fail),
        approval=AutoApprove(),
        capability=capability_state,
        namespace="payment",
        deployment="web",
    )
    await restart_deployment(ctx)
    return ctx


async def understand_demo() -> None:
    from aoip.capabilities.understand_host import understand_host
    from aoip.discovery_backend import MockHostDiscoveryBackend
    from aoip.system_model import SystemModel
    from aoip.understanding import UnderstandingContext

    ctx = UnderstandingContext(
        host="web-01",
        scope="payment/web-01",
        backend=MockHostDiscoveryBackend(),
        capability=CapabilityState(capability_id="understand_host", scope="payment/web-01"),
        model=SystemModel(scope="payment/web-01"),
    )
    await understand_host(ctx)
    print("\n=== understand_host (Day-1: Observe→Map→Ask, mock backend) ===")
    for line in ctx.trace:
        print("  " + line)
    print(f"  SystemModel facts: {[f.triple for f in ctx.model.facts]}")
    for c in ctx.communications:
        print(f"  ❓ INTERVIEW: {c.question}")


async def understand_real_profile_demo() -> None:
    """Cùng pipeline understand_host, nhưng nuốt VMProfile shape THẬT (discovery)."""
    from aoip.capabilities.understand_host import understand_host
    from aoip.discovery_backend import VMProfileDiscoveryBackend
    from aoip.system_model import SystemModel
    from aoip.understanding import UnderstandingContext

    profile = {
        "hostname": "db-01",
        "role": "database_server",
        "services": [
            {"name": "mariadb", "status": "running"},
            {"name": "nginx", "status": "running"},
            {"name": "cron", "status": "running"},
        ],
        "listeners": [
            {"port": 3306, "service": "mariadbd"},
            {"port": 80, "service": "nginx"},
            {"port": 9999, "service": ""},
        ],
    }
    ctx = UnderstandingContext(
        host="db-01",
        scope="acme/db-01",
        backend=VMProfileDiscoveryBackend(profile),
        capability=CapabilityState(capability_id="understand_host", scope="acme/db-01"),
        model=SystemModel(scope="acme/db-01"),
    )
    await understand_host(ctx)
    print("\n=== understand_host (REAL VMProfile → SystemModel + Interview) ===")
    for line in ctx.trace:
        print("  " + line)
    print(f"  SystemModel facts: {[f.triple for f in ctx.model.facts]}")
    for c in ctx.communications:
        print(f"  ❓ INTERVIEW: {c.question}")


async def main() -> None:
    cap = CapabilityState(capability_id="restart_deployment", scope="payment/web")
    print(f"START  score={cap.score:.4f} E={cap.dimensions['E']} maturity={cap.maturity.value}")
    for i in range(1, 4):
        ctx = await run_once(cap)
        cap = ctx.capability
        print(f"\n── run {i} ──")
        for line in ctx.trace:
            print("  " + line)
        print(
            f"  RESULT action={ctx.action.state.value} "
            f"score={cap.score:.4f} maturity={cap.maturity.value}"
        )
    print(f"\nEND    score={cap.score:.4f} maturity={cap.maturity.value}")
    await understand_demo()
    await understand_real_profile_demo()
    await inspect_host_demo()
    await map_system_graph_demo()


async def map_system_graph_demo() -> None:
    """Discovery → Fact → System Graph: AI dựng topology + tự thấy Unknown Edge."""
    from aoip.capabilities.map_system_graph import map_system_graph
    from aoip.discovery_backend import VMProfileDiscoveryBackend
    from aoip.system_model import SystemModel
    from aoip.understanding import UnderstandingContext

    profile = {
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}, {"name": "redis", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}, {"port": 6379, "service": "redis"}],
        "relationships": [
            {"source": "nginx", "relation": "proxies_to", "target": "payment-api", "evidence": "nginx.upstream"},
            {"source": "payment-api", "relation": "depends_on", "target": "redis", "evidence": "env.REDIS_HOST"},
            {"source": "payment-api", "relation": "depends_on", "target": "postgres", "evidence": "env.DB_HOST"},
        ],
    }
    ctx = UnderstandingContext(
        host="web-01",
        scope="acme/web-01",
        backend=VMProfileDiscoveryBackend(profile),
        capability=CapabilityState(capability_id="map_system_graph", scope="acme/web-01"),
        model=SystemModel(scope="acme/web-01"),
    )
    await map_system_graph(ctx)
    print("\n=== map_system_graph (Discovery → Fact → System Graph) ===")
    for line in ctx.trace:
        print("  " + line)
    print("  GRAPH edges:")
    for e in ctx.model.edges:
        print(f"    {e.subject} --{e.predicate}--> {e.obj}")
    for t in sorted(ctx.model.unknown_edge_targets):
        print(f"  ⚠️ UNKNOWN EDGE TARGET (chưa quan sát): {t} → hạt giống câu hỏi kiến trúc")


async def inspect_host_demo() -> None:
    """Observe → Expectation → Probe → Compare → Finding (reasoning của Senior)."""
    from aoip.capabilities.inspect_host import inspect_host
    from aoip.discovery_backend import VMProfileDiscoveryBackend
    from aoip.system_model import SystemModel
    from aoip.understanding import UnderstandingContext

    # nginx kỳ vọng 80+443 nhưng host chỉ listen 80 → một kỳ vọng hụt → câu hỏi.
    profile = {
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}, {"name": "redis", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}, {"port": 6379, "service": "redis"}],
    }
    ctx = UnderstandingContext(
        host="web-01",
        scope="acme/web-01",
        backend=VMProfileDiscoveryBackend(profile),
        capability=CapabilityState(capability_id="inspect_host", scope="acme/web-01"),
        model=SystemModel(scope="acme/web-01"),
    )
    await inspect_host(ctx)
    print("\n=== inspect_host (Observe→Expectation→Probe→Compare→Finding) ===")
    for line in ctx.trace:
        print("  " + line)
    for f in ctx.findings:
        mark = "✅" if f.verdict else "⚠️"
        print(f"  {mark} FINDING: {f.claim}")
    for c in ctx.communications:
        print(f"  ❓ INTERVIEW: {c.question}")


if __name__ == "__main__":
    asyncio.run(main())
