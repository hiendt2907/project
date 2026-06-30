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
    await mission_demo()


async def mission_demo() -> None:
    """PIVOT: runtime chạy theo MISSION — capability = composition tự điều phối.

    Không còn run_discovery()/run_completion()/run_reasoning() rời rạc; chỉ khai
    báo Mission 'understand host/tenant', runtime tự compose. KPI = Mission
    Completion (% hiểu), đơn vị giá trị khách hàng thấy.
    """
    from aoip.capabilities.missions import understand_host_mission
    from aoip.discovery_backend import VMProfileDiscoveryBackend
    from aoip.evidence import (
        EvidenceCompletionEngine,
        InferenceResolver,
        RuntimeResolver,
    )
    from aoip.system_model import SystemModel
    from aoip.understanding import UnderstandingContext

    def _ctx(profile: dict, host: str) -> UnderstandingContext:
        return UnderstandingContext(
            host=host,
            scope=f"acme/{host}",
            backend=VMProfileDiscoveryBackend(profile),
            capability=CapabilityState(capability_id="understand_host", scope=f"acme/{host}"),
            model=SystemModel(scope=f"acme/{host}"),
        )

    web = _ctx({
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}, {"name": "redis", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}, {"port": 6379, "service": "redis"}],
        "relationships": [
            {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
            {"source": "payment-api", "relation": "reads", "target_type": "database", "target": "orders"},
        ],
    }, "web-01")
    db = _ctx({
        "hostname": "db-01",
        "services": [{"name": "redis", "status": "running"}],
        "listeners": [{"port": 6379, "service": "redis"}],
        "relationships": [{"source": "redis", "relation": "depends_on", "target": "mystery-svc"}],
    }, "db-01")

    # web-01: runtime tìm được mọi gap; db-01: 'mystery-svc' không nguồn nào giải.
    # Engine factory chọn prober theo host đang chạy (cờ trên context).
    def _engine_factory_for(ctx) -> EvidenceCompletionEngine:
        prober = (lambda n: "host:elsewhere") if ctx.host == "web-01" else (lambda n: None)
        return EvidenceCompletionEngine([InferenceResolver(), RuntimeResolver(prober=prober)])

    # Per-host engine khác nhau → chạy host mission riêng rồi gộp tenant thủ công
    # (understand_tenant_mission dùng cho engine đồng nhất; ở đây minh hoạ KPI/host).
    web_m = await understand_host_mission(web, engine=_engine_factory_for(web),
                                          parent_mission_id="understand_tenant:acme")
    db_m = await understand_host_mission(db, engine=_engine_factory_for(db),
                                         parent_mission_id="understand_tenant:acme")
    from aoip.mission import Mission, MissionState, aggregate_completion
    subs = [web_m, db_m]
    completion = aggregate_completion(subs)
    state = MissionState.COMPLETED if all(s.state is MissionState.COMPLETED for s in subs) else MissionState.BLOCKED
    tenant = (Mission(mission_id="understand_tenant:acme", goal="understand_tenant", scope="acme")
              .to(MissionState.PLANNED).to(MissionState.ASSIGNED).to(MissionState.IN_PROGRESS)
              .to(state, completion=completion))

    print("\n=== MISSION RUNTIME (capability = self-composed mission) ===")
    for m in subs:
        print(f"  Mission {m.goal} [{m.scope}] → {m.state.value} "
              f"completion={m.completion:.0%}  DoD✗={list(m.dod_failed)}")
    for c in db.communications:
        print(f"    ❓ (db-01 còn hỏi): {c.question}")
    print(f"  TENANT Mission Completion: {tenant.state.value} → {tenant.completion:.0%} "
          f"(KPI khách hàng thấy: 'AI hiểu tenant acme {tenant.completion:.0%}')")


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
        # Knowledge Graph đa-loại: hạ tầng + mạng + dữ liệu + sở hữu trong MỘT đồ thị.
        "relationships": [
            {"source": "nginx", "relation": "proxies_to", "target": "payment-api", "evidence": "nginx.upstream"},
            {"source_type": "service", "source": "payment-api", "relation": "runs_on",
             "target_type": "host", "target": "web-01", "evidence": "agent.host"},
            {"source_type": "service", "source": "payment-api", "relation": "reads",
             "target_type": "database", "target": "postgres", "evidence": "env.DB_HOST"},
            {"source_type": "service", "source": "payment-api", "relation": "depends_on",
             "target": "redis", "evidence": "env.REDIS_HOST"},
            {"source_type": "team", "source": "payments", "relation": "owns",
             "target_type": "service", "target": "payment-api", "evidence": "codeowners"},
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
    print("\n=== map_system_graph (Discovery → Fact → Knowledge Graph) ===")
    for line in ctx.trace:
        print("  " + line)
    print("  GRAPH edges:")
    for e in ctx.model.edges:
        print(f"    {e.subject} --{e.predicate}--> {e.obj}")
    print(f"  PROJECTION ownership: {[e.triple for e in ctx.model.project('owns')]}")
    print(f"  PROJECTION data-access: {[e.triple for e in ctx.model.project('reads', 'writes')]}")
    for t in sorted(ctx.model.unknown_edge_targets):
        print(f"  ⚠️ UNKNOWN NODE (chưa quan sát): {t} → đưa vào Evidence Completion")

    # ── Slice 4: Evidence Completion — exhaust suy luận TRƯỚC khi hỏi người ──
    from aoip.evidence import (
        DocumentResolver,
        EvidenceCompletionEngine,
        InferenceResolver,
        PeerHostResolver,
        RuntimeResolver,
        complete_evidence,
    )

    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        # runtime "tìm thấy" postgres ở host khác (vd DNS/k8s lookup).
        RuntimeResolver(prober=lambda n: "host:db-02" if n == "db:postgres" else None),
        DocumentResolver(index={}),
        PeerHostResolver(registry={}),
    ])
    report = await complete_evidence(ctx, engine)
    print("\n=== Evidence Completion (INV_INFER_BEFORE_ASK) ===")
    print(f"  KPI: {report.resolved_count}/{report.total_gaps} tự giải, "
          f"{report.asked_count} câu hỏi (rate={report.inference_rate:.2f})")
    for node, method in report.resolved.items():
        print(f"  ✅ RESOLVED {node} bằng [{method}] — không cần hỏi người")
    for c in ctx.communications:
        print(f"  ❓ INTERVIEW (đã exhaust): {c.question}")


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
