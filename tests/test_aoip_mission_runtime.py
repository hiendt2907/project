"""Tests pivot chiến lược: runtime chạy theo MISSION, không phải gọi từng feature.

Mỗi capability (understand_host/…/understand_tenant) = một Mission: composition
các primitive/runtime đã có, đánh giá bằng Definition-of-Done. KPI = Mission
Completion (% hiểu host/service/tenant) — đơn vị giá trị khách hàng thấy, không
phải inference rate. Mission đã là Runtime noun (lifecycle trong SEMANTIC_RULES);
KHÔNG noun mới.
"""
from __future__ import annotations

import pytest

from aoip.discovery_backend import VMProfileDiscoveryBackend
from aoip.capability import CapabilityState
from aoip.evidence import (
    EvidenceCompletionEngine,
    InferenceResolver,
    RuntimeResolver,
)
from aoip.mission import Mission, MissionState, run_mission
from aoip.capabilities.missions import understand_host_mission, understand_tenant_mission
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


# Host hiểu trọn vẹn: mọi gap topology giải được bằng runtime → DoD pass hết.
_FULL = {
    "hostname": "web-01",
    "services": [{"name": "nginx", "status": "running"}, {"name": "redis", "status": "running"}],
    "listeners": [{"port": 80, "service": "nginx"}, {"port": 6379, "service": "redis"}],
    "relationships": [
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
        {"source": "payment-api", "relation": "reads", "target_type": "database", "target": "orders"},
    ],
}


def _engine_resolving_all() -> EvidenceCompletionEngine:
    return EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=lambda n: "host:elsewhere"),  # mọi gap đều định vị được
    ])


def test_mission_lifecycle_rejects_illegal_transition():
    m = Mission(mission_id="m1", goal="understand_host", scope="acme/web-01")
    assert m.state is MissionState.CREATED
    with pytest.raises(ValueError):
        m.to(MissionState.COMPLETED)  # CREATED → COMPLETED là cấm (skip)


async def test_understand_host_mission_completes_when_dod_passes():
    ctx = _ctx(_FULL, "web-01")
    mission = await understand_host_mission(ctx, engine=_engine_resolving_all())

    assert mission.state is MissionState.COMPLETED
    assert mission.completion == pytest.approx(1.0)
    assert mission.dod_failed == ()
    # không còn câu hỏi tồn đọng (đã exhaust + giải hết).
    assert ctx.communications == []


async def test_mission_blocked_when_gap_unresolved_leaves_question():
    # engine KHÔNG giải được gap → còn câu hỏi → DoD 'no_pending_questions' fail.
    ctx = _ctx(_FULL, "web-01")
    engine = EvidenceCompletionEngine([InferenceResolver()])  # không runtime/doc
    mission = await understand_host_mission(ctx, engine=engine)

    assert mission.state is MissionState.BLOCKED
    assert mission.completion < 1.0
    assert "no_pending_questions" in mission.dod_failed
    assert len(ctx.communications) >= 1


async def test_tenant_mission_aggregates_host_completion():
    # Hai host: một hiểu trọn, một còn lỗ hổng → tenant completion = trung bình.
    full = _ctx(_FULL, "web-01")
    partial_profile = {
        "hostname": "db-01",
        "services": [{"name": "redis", "status": "running"}],
        "listeners": [{"port": 6379, "service": "redis"}],
        "relationships": [
            {"source": "redis", "relation": "depends_on", "target": "mystery-svc"},
        ],
    }
    partial = _ctx(partial_profile, "db-01")

    tenant, subs = await understand_tenant_mission(
        tenant_scope="acme",
        contexts=[full, partial],
        engine_factory=lambda: EvidenceCompletionEngine([InferenceResolver()]),
    )
    assert len(subs) == 2
    assert tenant.goal == "understand_tenant"
    # full host pass hết DoD; partial còn câu hỏi → tenant ở giữa.
    assert 0.0 < tenant.completion < 1.0
    assert tenant.state is MissionState.BLOCKED  # còn host chưa hiểu trọn
    # sub-mission tham chiếu parent.
    assert all(s.parent_mission_id == tenant.mission_id for s in subs)


async def test_run_mission_executes_plan_and_scores_dod():
    ctx = _ctx(_FULL, "web-01")
    calls: list[str] = []

    async def step_a(c):
        calls.append("a")

    async def step_b(c):
        calls.append("b")

    m = Mission(mission_id="m", goal="g", scope="s")
    done = await run_mission(
        m, ctx,
        plan=[step_a, step_b],
        dod=[("always", lambda c: True), ("never", lambda c: False)],
    )
    assert calls == ["a", "b"]
    assert done.completion == pytest.approx(0.5)
    assert done.state is MissionState.BLOCKED
    assert "always" in done.dod_passed and "never" in done.dod_failed
