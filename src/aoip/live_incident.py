"""Live incident demo — sự cố THẬT trên VM thật → reasoning blast radius (EPIC Operate).

    python -m aoip.live_incident

Dừng redis THẬT trên cust-db, agent probe THẬT thấy down, rồi suy ra ai bị ảnh
hưởng bằng Knowledge Graph (KHÔNG GPT), đề xuất Recovery Mission. Cuối cùng khởi
động lại redis. Đây là thứ khách hàng trả tiền: AI hiểu sự cố, không chỉ biết
topology.
"""
from __future__ import annotations

import asyncio

from aoip.capability import CapabilityState
from aoip.incident import investigate_incident_mission
from aoip.remote_linux_backend import RemoteLinuxBackend
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.transport import OrbTransport
from aoip.understanding import UnderstandingContext


def _tenant_graph() -> SystemModel:
    # Chuỗi phụ thuộc tenant (đã tích lũy qua discovery + interview ở các mission trước).
    edges = infer_edges([
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
        {"source": "payment-api", "relation": "depends_on", "target": "cust-db"},
    ])
    return SystemModel(scope="acme").fold(*edges)


async def _orb(vm: str, *argv: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "orb", "-m", vm, *argv,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def run() -> None:
    backend = RemoteLinuxBackend(OrbTransport("cust-db"))

    async def probe(_node: str) -> bool:
        # node svc:cust-db ↔ redis trên host cust-db:6379 (probe /dev/tcp THẬT).
        return await backend.probe_port("cust-db", 6379)

    print("=== LIVE INCIDENT — sự cố THẬT trên cust-db ===\n")
    print(f"[pre] redis reachable? {await probe('svc:cust-db')}")

    print("[chaos] dừng redis-server trên cust-db (THẬT)...")
    await _orb("cust-db", "sudo", "systemctl", "stop", "redis-server")
    await asyncio.sleep(1)
    print(f"[post-chaos] redis reachable? {await probe('svc:cust-db')}")

    ctx = UnderstandingContext(
        host="cust-db", scope="acme/cust-db", backend=backend,
        capability=CapabilityState(capability_id="investigate_incident", scope="acme/cust-db"),
        model=_tenant_graph(),
    )
    try:
        mission = await investigate_incident_mission(
            ctx, failed_node="svc:cust-db", symptom="redis connection timeout", probe=probe,
        )
        print(f"\n[mission] {mission.goal} → {mission.state.value} completion={mission.completion:.0%}")
        for line in ctx.trace:
            print("  " + line)
        for f in ctx.findings:
            print(f"  {'⚠️' if f.verdict else '✓'} FINDING: {f.claim}")

        # ── Diagnosis Engine: nhiều giả thuyết root-cause + falsification (THẬT) ──
        from aoip.diagnosis import diagnose
        from aoip.sre_diagnosis import sre_root_cause_candidates
        cands = sre_root_cause_candidates(
            "svc:cust-db", "cust-db", backend._t, port=6379, service="redis-server")
        diag = await diagnose(cands)
        ctx.diagnosis_confidence = diag.confidence
        print("\n  DIAGNOSIS (multi-hypothesis + falsification):")
        for f in diag.findings:
            print(f"    ✓ ROOT CAUSE: {f.claim}  (conf={f.confidence})")
        for r in diag.rejected:
            print(f"    ✗ bác bỏ: {r}")
        print(f"    → Diagnosis Confidence = {diag.confidence}")

        # ── Decision layer: Incident → Candidate Actions → Decision (chưa execute) ──
        from aoip.decision import decide_recovery, generate_candidates
        print("\n  CANDIDATE ACTIONS (phương án phục hồi):")
        for c in generate_candidates("svc:cust-db"):
            mark = "✦" if c.resolves else "·"
            print(f"    {mark} {c.action.plan}  [risk={c.risk} conf={c.confidence}]")
        decision = decide_recovery(ctx, failed_node="svc:cust-db",
                                   diagnosis_confidence=ctx.diagnosis_confidence or 1.0)
        chosen = ctx.actions[0]
        print(f"\n  🧭 DECISION: {decision.goal}")
        print(f"     chọn → {chosen.action.decision_goal if hasattr(chosen,'action') else chosen.decision_goal}: {chosen.plan}")
        print(f"     confidence={ctx.recovery_confidence}  state={chosen.state.value} "
              f"(chờ human approval — fail-closed, KHÔNG tự execute)")
    finally:
        print("\n[recover] khởi động lại redis-server (cleanup)...")
        await _orb("cust-db", "sudo", "systemctl", "start", "redis-server")
        await asyncio.sleep(1)
        print(f"[verify] redis reachable? {await probe('svc:cust-db')}")


if __name__ == "__main__":
    asyncio.run(run())
