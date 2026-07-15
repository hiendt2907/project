"""Capability Missions — understand_host / understand_tenant như những Mission.

Pivot: thay vì gọi tuần tự run_discovery()/run_completion()/run_reasoning(), mỗi
capability là một Mission compose các runtime step ĐÃ CÓ (Observe→Verify→Graph→
Evidence Completion) và tự chấm Definition-of-Done. understand_tenant sinh sub-
Mission per host (SEMANTIC: Mission produces sub-Mission) và gộp completion.

KHÔNG noun mới: Mission/Fact/Hypothesis/Communication đều đã có. DoD = tiêu chí
field của Mission; Mission Completion = Derived KPI khách hàng thấy.
"""
from __future__ import annotations

from aoip.evidence import EvidenceCompletionEngine, complete_evidence
from aoip.mission import (
    DoDCheck,
    Mission,
    MissionState,
    MissionStep,
    aggregate_completion,
    run_mission,
)
from aoip.understanding import (
    hypothesize_services,
    infer_topology,
    model_host,
    observe_host,
    verify_services,
)


def _host_plan(engine: EvidenceCompletionEngine) -> list[MissionStep]:
    """Composition runtime cho 'hiểu một host' — Observe→Verify→Graph→Complete."""

    async def _complete(ctx) -> None:
        await complete_evidence(ctx, engine)

    return [
        observe_host,          # Observe: discovery (inventory + topology hints)
        hypothesize_services,  # mỗi service → tuyên bố cổng
        verify_services,       # probe → Fact (node quan sát được)
        infer_topology,        # suy ra edge quan hệ → Knowledge Graph
        model_host,            # fold node + edge → SystemModel
        _complete,             # Evidence Completion (infer-before-ask) lấp gap
    ]


def _host_dod() -> list[DoDCheck]:
    """Definition-of-Done cho understand_host — đo 'đã hiểu host đến đâu'."""
    return [
        ("host_observed", lambda c: len(c.observations) >= 1),
        ("model_nonempty", lambda c: len(c.model.facts) > 0),
        # mọi node được nhắc trong graph đều đã định vị (không còn lỗ hổng kiến trúc).
        ("graph_resolved", lambda c: len(c.model.unknown_edge_targets) == 0),
        # đã exhaust evidence → không còn câu hỏi tồn đọng cho người.
        ("no_pending_questions", lambda c: len(c.communications) == 0),
    ]


async def understand_host_mission(
    ctx,
    *,
    engine: EvidenceCompletionEngine,
    mission_id: str | None = None,
    parent_mission_id: str | None = None,
    mission_store=None,
    tenant_id: str | None = None,
) -> Mission:
    """Chạy capability 'understand_host' như một Mission, trả Mission đã chấm DoD."""
    mission = Mission(
        mission_id=mission_id or f"understand_host:{ctx.host}",
        goal="understand_host",
        scope=ctx.scope,
        parent_mission_id=parent_mission_id,
    )
    result = await run_mission(mission, ctx, plan=_host_plan(engine), dod=_host_dod())
    if mission_store is not None and tenant_id is not None:
        await mission_store.save(
            tenant_id, result, last_activity="understand_host completed",
            next_action="collect more evidence" if result.state is not MissionState.COMPLETED else None,
        )
    return result


async def understand_tenant_mission(
    *,
    tenant_scope: str,
    contexts: list,
    engine_factory,
    mission_store=None,
) -> tuple[Mission, list[Mission]]:
    """Mission cấp tenant: sinh sub-Mission per host, gộp Mission Completion.

    Đây là đơn vị giá trị cuối: '% hiểu tenant' = trung bình '% hiểu host'. Tenant
    COMPLETED chỉ khi MỌI host hiểu trọn; còn host BLOCKED → tenant BLOCKED.
    """
    tenant_id = f"understand_tenant:{tenant_scope}"
    subs: list[Mission] = []
    for ctx in contexts:
        sub = await understand_host_mission(
            ctx, engine=engine_factory(), parent_mission_id=tenant_id,
            mission_store=mission_store, tenant_id=tenant_scope,
        )
        subs.append(sub)

    completion = aggregate_completion(subs)
    all_done = all(s.state is MissionState.COMPLETED for s in subs)
    tenant = Mission(
        mission_id=tenant_id,
        goal="understand_tenant",
        scope=tenant_scope,
    )
    tenant = tenant.to(MissionState.PLANNED).to(MissionState.ASSIGNED).to(
        MissionState.IN_PROGRESS
    )
    final = MissionState.COMPLETED if all_done else MissionState.BLOCKED
    final_mission = tenant.to(
            final,
            completion=completion,
            dod_passed=tuple(s.mission_id for s in subs if s.state is MissionState.COMPLETED),
            dod_failed=tuple(s.mission_id for s in subs if s.state is not MissionState.COMPLETED),
        )
    if mission_store is not None:
        await mission_store.save(
            tenant_scope, final_mission, last_activity="understand_tenant completed",
            next_action="resolve blocked host missions" if final is MissionState.BLOCKED else None,
        )
    return final_mission, subs
