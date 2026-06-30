"""Tests EPIC Operate (1): Incident Understanding — reasoning trên Knowledge Graph.

Đây là lúc graph trả tiền: service chết → suy ra BLAST RADIUS (ai bị ảnh hưởng)
bằng traversal dependents, KHÔNG GPT. Vòng: Observe→Verify→Impact→Hypothesis→
recommend Recovery Mission. Tái dùng Observation/Finding/Hypothesis/Mission/
SystemModel — KHÔNG noun mới.
"""
from __future__ import annotations

import pytest

from aoip.capability import CapabilityState
from aoip.incident import investigate_incident_mission
from aoip.mission import MissionState
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def _tenant_graph() -> SystemModel:
    # nginx → payment-api → cust-db (redis). Chuỗi phụ thuộc thật của tenant.
    edges = infer_edges([
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
        {"source": "payment-api", "relation": "depends_on", "target": "cust-db"},
        {"source": "reporting", "relation": "depends_on", "target": "cust-db"},
    ])
    return SystemModel(scope="acme").fold(*edges)


def test_blast_radius_transitive_dependents():
    m = _tenant_graph()
    # cust-db chết → payment-api + reporting (trực tiếp) + nginx (gián tiếp).
    affected = set(m.blast_radius("svc:cust-db"))
    assert affected == {"svc:payment-api", "svc:reporting", "svc:nginx"}
    # lá không kéo theo ai.
    assert m.blast_radius("svc:nginx") == ()


def _ctx() -> UnderstandingContext:
    return UnderstandingContext(
        host="cust-db", scope="acme/cust-db", backend=None,
        capability=CapabilityState(capability_id="investigate_incident", scope="acme/cust-db"),
        model=_tenant_graph(),
    )


async def test_incident_mission_confirms_failure_and_impact():
    ctx = _ctx()
    # probe THẬT (inject): cust-db không phản hồi → down.
    mission = await investigate_incident_mission(
        ctx, failed_node="svc:cust-db", symptom="redis timeout",
        probe=lambda node: False,  # False = không reachable = DOWN
    )
    assert mission.goal == "investigate_incident"
    assert mission.state is MissionState.COMPLETED
    # Finding xác nhận DOWN.
    assert any(f.verdict and "DOWN" in f.claim for f in ctx.findings)
    # Finding blast radius liệt kê đúng các service bị ảnh hưởng.
    impact = [f for f in ctx.findings if "blast radius" in f.claim.lower()]
    assert impact and "svc:payment-api" in impact[0].claim


async def test_incident_recommends_recovery_mission():
    ctx = _ctx()
    await investigate_incident_mission(
        ctx, failed_node="svc:cust-db", symptom="redis timeout", probe=lambda n: False,
    )
    # Hypothesis/đề xuất Recovery Mission cho node hỏng (chưa execute — epic sau).
    assert any("recover_service:svc:cust-db" in h.claim for h in ctx.hypotheses)


async def test_healthy_node_is_not_an_incident():
    ctx = _ctx()
    mission = await investigate_incident_mission(
        ctx, failed_node="svc:cust-db", symptom="false alarm", probe=lambda n: True,  # reachable
    )
    # Probe khỏe → KHÔNG kết luận sự cố, KHÔNG đề xuất recovery (never assume).
    assert not any(f.verdict and "DOWN" in f.claim for f in ctx.findings)
    assert not any("recover_service" in h.claim for h in ctx.hypotheses)
    assert mission.state is MissionState.COMPLETED  # điều tra xong, kết luận healthy
