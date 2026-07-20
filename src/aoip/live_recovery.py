"""Live controlled recovery — vòng phục hồi ĐẦY ĐỦ trên VM thật (EPIC Operate).

    python -m aoip.live_recovery

Sự cố THẬT (dừng redis trên cust-db) → verify → diagnosis → decision → plan
(process_down + systemd) → HUMAN APPROVAL → execute restart → verify service +
dependents → complete, toàn bộ ghi audit hash-chain. Sau đó chứng minh các ca
FAIL-CLOSED ngay trên hạ tầng thật: thiếu approval / service healthy / diagnosis
stale đều ZERO mutation. Đây là ranh giới: AI phục hồi có kiểm soát, bằng chứng,
trách nhiệm — KHÔNG còn chỉ "hiểu".
"""
from __future__ import annotations

import asyncio

from aoip import audit
from aoip.capability import CapabilityState
from aoip.capability_diagnosis import capability_root_cause_candidates
from aoip.diagnosis import diagnose
from aoip.incident import investigate_incident_mission
from aoip.objects import ActionState
from aoip.recovery import (
    Approval,
    RecoveryGate,
    RecoveryRequest,
    execute_recovery,
    plan_recovery,
)
from aoip.remote_linux_backend import RemoteLinuxBackend
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.transport import OrbTransport
from aoip.understanding import UnderstandingContext

AUDIT_PATH = "/tmp/aoip_recovery_audit.jsonl"
DEP_PROBE = {  # dependent node → (vm, port) để verify hết ảnh hưởng
    "svc:payment-api": ("cust-app", 8080),
    "svc:nginx": ("cust-edge", 80),
}


def _graph() -> SystemModel:
    edges = infer_edges([
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
        {"source": "payment-api", "relation": "depends_on", "target": "cust-db"},
    ])
    return SystemModel(scope="acme").fold(*edges)


async def _orb(vm: str, *argv: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "orb", "-m", vm, *argv,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate()


def _gate() -> RecoveryGate:
    return RecoveryGate(
        allowed_failure_modes=frozenset({"process_down"}),
        allowed_substrates=frozenset({"systemd"}),
        max_risk=0.5, scope_prefix="svc:",
        min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
        allowed_targets=frozenset({"redis-server"}))


async def _diagnose(ctx, backend) -> None:
    cands = capability_root_cause_candidates(
        "svc:cust-db", "cust-db", backend._t, port=6379, service="redis-server")
    diag = await diagnose(cands)
    ctx.diagnosis_confidence = diag.confidence
    print(f"  diagnosis: findings={[f.claim.split(': ',1)[-1] for f in diag.findings]} "
          f"score={diag.confidence}")


async def _probe_dependent(node: str) -> bool:
    vm, port = DEP_PROBE[node]
    return await RemoteLinuxBackend(OrbTransport(vm)).probe_port(vm, port)


def _fresh_ctx(backend) -> UnderstandingContext:
    return UnderstandingContext(
        host="cust-db", scope="acme/cust-db", backend=backend,
        capability=CapabilityState(capability_id="recover_service", scope="acme/cust-db"),
        model=_graph())


async def run() -> None:
    backend = RemoteLinuxBackend(OrbTransport("cust-db"))
    gate = _gate()
    log = audit.FileAuditLog(AUDIT_PATH)
    now = 10_000.0  # đồng hồ logic (demo) — diagnosed_at gắn cùng mốc để tươi

    async def probe(_n="svc:cust-db") -> bool:
        return await backend.probe_port("cust-db", 6379)

    print("=== LIVE CONTROLLED RECOVERY — VM thật cust-db ===\n")
    print("[chaos] dừng redis-server trên cust-db (THẬT)...")
    await _orb("cust-db", "sudo", "systemctl", "stop", "redis-server")
    await asyncio.sleep(1)
    print(f"[post-chaos] redis reachable? {await probe()}\n")

    # ── 1. Verified incident → diagnosis ─────────────────────────────────────
    ctx = _fresh_ctx(backend)
    await investigate_incident_mission(ctx, failed_node="svc:cust-db",
                                       symptom="redis connection timeout", probe=probe)
    await _diagnose(ctx, backend)
    affected = list(ctx.model.blast_radius("svc:cust-db"))
    print(f"  blast radius (dependents verify sau recover): {affected}")

    # ── 2. Plan recovery (failure_mode+substrate) + HUMAN APPROVAL ───────────
    action = plan_recovery(failed_node="svc:cust-db", failure_mode="process_down",
                           substrate="systemd", unit="redis-server", port=6379, risk=0.3)
    print(f"\n  PLAN: {action.plan}  [state={action.state.value}]")
    approved_action = action.at(ActionState.APPROVED)
    approval = Approval(approved=True, approver="human:on-call", action_scope=action.scope)
    req = RecoveryRequest(
        failed_node="svc:cust-db", failure_mode="process_down", substrate="systemd",
        unit="redis-server", port=6379, action=approved_action, risk=0.3,
        diagnosed_at=now, dependents=tuple(affected))
    print(f"  APPROVAL: {approval.approver} ✓ (HITL — fail-closed)")

    # ── 3. Execute controlled loop (THẬT: sudo systemctl restart) ────────────
    print("\n  ▶ EXECUTE controlled recovery loop:")
    outcome = await execute_recovery(
        ctx, req=req, transport=backend._t, audit_log=log, gate=gate, approval=approval,
        env_auto_execute=False, now=now, probe_dependent=_probe_dependent)
    for line in ctx.trace[-4:]:
        print("    " + line)
    print(f"\n  ✅ OUTCOME: {outcome.status} — {outcome.reason}")
    print(f"     action={outcome.action.state.value}  evidence={outcome.evidence}")

    # ── 4. Chứng minh FAIL-CLOSED ngay trên VM thật (ZERO mutation) ──────────
    print("\n=== FAIL-CLOSED PROOFS (zero mutation trên hạ tầng thật) ===")

    # (a) thiếu approval
    await _orb("cust-db", "sudo", "systemctl", "stop", "redis-server")
    await asyncio.sleep(1)
    ctx_a = _fresh_ctx(backend)
    await investigate_incident_mission(ctx_a, failed_node="svc:cust-db",
                                       symptom="redis timeout", probe=probe)
    await _diagnose(ctx_a, backend)
    out_a = await execute_recovery(
        ctx_a, req=RecoveryRequest(
            failed_node="svc:cust-db", failure_mode="process_down", substrate="systemd",
            unit="redis-server", port=6379, action=approved_action, risk=0.3,
            diagnosed_at=now, dependents=()),
        transport=backend._t, audit_log=log, gate=gate,
        approval=Approval(approved=False, approver="-", action_scope=action.scope),
        env_auto_execute=False, now=now)
    print(f"  (a) thiếu approval → {out_a.status}; redis vẫn DOWN? {not await probe()}")

    # (b) diagnosis stale (cùng sự cố nhưng chẩn đoán quá cũ)
    out_b = await execute_recovery(
        ctx_a, req=RecoveryRequest(
            failed_node="svc:cust-db", failure_mode="process_down", substrate="systemd",
            unit="redis-server", port=6379, action=approved_action, risk=0.3,
            diagnosed_at=now - 9_999, dependents=()),
        transport=backend._t, audit_log=log, gate=gate, approval=approval,
        env_auto_execute=False, now=now)
    print(f"  (b) diagnosis stale → {out_b.status}; redis vẫn DOWN? {not await probe()}")

    # khôi phục để chứng minh ca (c) healthy
    await _orb("cust-db", "sudo", "systemctl", "start", "redis-server")
    await asyncio.sleep(1)
    ctx_c = _fresh_ctx(backend)
    ctx_c.diagnosis_confidence = 0.8
    ctx_c.findings.append(_down_finding())  # giả định cảnh báo cũ, nhưng state đã healthy
    out_c = await execute_recovery(
        ctx_c, req=RecoveryRequest(
            failed_node="svc:cust-db", failure_mode="process_down", substrate="systemd",
            unit="redis-server", port=6379, action=approved_action, risk=0.3,
            diagnosed_at=now, dependents=()),
        transport=backend._t, audit_log=log, gate=gate, approval=approval,
        env_auto_execute=False, now=now)
    print(f"  (c) service healthy → {out_c.status}; redis reachable? {await probe()}")

    print(f"\n[audit] chain verified={log.verify_chain()}  events={log.events()}")
    print(f"[audit] file: {AUDIT_PATH}")


def _down_finding():
    from aoip.objects import Finding
    return Finding(claim="svc:cust-db is DOWN (probe failed)", references=("stale",),
                   verdict=True, confidence=0.9)


if __name__ == "__main__":
    asyncio.run(run())
