"""Living loop demo — Interview → persistent Fact → Mission Resume → 0 câu hỏi.

    python -m aoip.live_interview [vm] [kb_path]

KPI sống còn (không phải test count): agent chạm host THẬT, hỏi người MỘT lần về
node không tự giải được, lưu câu trả lời thành Fact BỀN, rồi lần chạy sau (tiến
trình store MỚI = mô phỏng reinstall) KHÔNG hỏi lại và hoàn thành mission.
"""
from __future__ import annotations

import asyncio
import sys

from aoip.capabilities.missions import understand_host_mission
from aoip.capability import CapabilityState
from aoip.evidence import EvidenceCompletionEngine, InferenceResolver, RuntimeResolver
from aoip.knowledge.store import FileKnowledgeStore, answer_question, seed_model
from aoip.remote_linux_backend import RemoteLinuxBackend
from aoip.system_model import SystemModel
from aoip.transport import OrbTransport
from aoip.understanding import UnderstandingContext


def _engine() -> EvidenceCompletionEngine:
    # KHÔNG peer resolver: node cross-host không tự giải → buộc phải hỏi người.
    return EvidenceCompletionEngine([InferenceResolver(), RuntimeResolver(prober=lambda n: None)])


async def _run_once(vm: str, tenant: str, store: FileKnowledgeStore):
    scope = f"{tenant}/{vm}"
    ctx = UnderstandingContext(
        host=vm, scope=scope, backend=RemoteLinuxBackend(OrbTransport(vm)),
        capability=CapabilityState(capability_id="understand_host", scope=scope),
        model=SystemModel(scope=scope),
    )
    # Nạp tri thức BỀN (câu trả lời người trước đây) TRƯỚC khi chạy → Mission Resume.
    known = store.load_facts(tenant, scope)
    ctx.model = seed_model(ctx.model, known)
    mission = await understand_host_mission(ctx, engine=_engine())
    return mission, ctx


async def run(vm: str, kb_path: str) -> None:
    tenant = "acme"
    print(f"=== LIVING LOOP — host THẬT {vm}, KB bền: {kb_path} ===\n")

    # ── RUN 1: chưa có tri thức người → agent hỏi ──
    store1 = FileKnowledgeStore(kb_path)
    m1, ctx1 = await _run_once(vm, tenant, store1)
    print(f"[run 1] mission={m1.state.value} completion={m1.completion:.0%} "
          f"questions={len(ctx1.communications)}")
    if not ctx1.communications:
        print("  (host này không có node cross-host chưa biết — thử cust-app)")
        return
    for c in ctx1.communications:
        print(f"  ❓ {c.question}")

    # ── HUMAN trả lời → Fact BỀN ──
    answers = {"svc:cust-db": "AWS RDS Aurora PostgreSQL (db.acme.internal:5432)"}
    learned = []
    for c in ctx1.communications:
        ans = answers.get(c.blocking_unknown, "out-of-scope managed service")
        fact = answer_question(c, ans)
        learned.append(fact)
        print(f"  🧑 human: {c.blocking_unknown} = {ans}")
    store1.save_facts(tenant, f"{tenant}/{vm}", learned)
    print(f"  💾 đã lưu {len(learned)} Fact bền vào KB")

    # ── RUN 2: tiến trình store MỚI (mô phỏng reinstall) → KHÔNG hỏi lại ──
    store2 = FileKnowledgeStore(kb_path)  # fresh instance, same file
    m2, ctx2 = await _run_once(vm, tenant, store2)
    print(f"\n[run 2 — sau reinstall/restart] mission={m2.state.value} "
          f"completion={m2.completion:.0%} questions={len(ctx2.communications)}")
    if not ctx2.communications:
        print("  ✅ HỌC THÀNH CÔNG: tri thức người sống sót, agent KHÔNG hỏi lại.")
    else:
        for c in ctx2.communications:
            print(f"  ❓ vẫn hỏi: {c.question}")


if __name__ == "__main__":
    vm = sys.argv[1] if len(sys.argv) > 1 else "cust-app"
    kb = sys.argv[2] if len(sys.argv) > 2 else "/tmp/aoip_kb_demo.json"
    asyncio.run(run(vm, kb))
