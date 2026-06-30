"""Tests EPIC Operate (2): Decision Engine — Incident → Candidate Actions → Decision.

Reviewer: Recovery phải nhận một DECISION, không nhận Incident trực tiếp. Tầng này
đúng ontology (Decision→Action, INV_DECISION_ACTION_SEPARATION) vốn bị bỏ quên.
Agent sinh NHIỀU phương án (restart/failover/drain/escalate/wait), chấm điểm, chọn
phương án rủi ro nhỏ nhất mà GIẢI QUYẾT được (INV_SMALL_BLAST_RADIUS) — KHÔNG
execute (fail-closed, chờ approval). Tái dùng Decision/Action — KHÔNG noun mới.
"""
from __future__ import annotations

import pytest

from aoip.capability import CapabilityState
from aoip.decision import decide_recovery, generate_candidates
from aoip.objects import ActionState, Decision, Finding
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def _ctx_with_incident() -> UnderstandingContext:
    model = SystemModel(scope="acme").fold(*infer_edges([
        {"source": "payment-api", "relation": "depends_on", "target": "cust-db"},
    ]))
    ctx = UnderstandingContext(
        host="cust-db", scope="acme/cust-db", backend=None,
        capability=CapabilityState(capability_id="decide_recovery", scope="acme/cust-db"),
        model=model,
    )
    ctx.findings.append(Finding(claim="svc:cust-db is DOWN (probe failed)",
                                references=("incident",), verdict=True, confidence=0.95))
    ctx.findings.append(Finding(claim="blast radius of svc:cust-db: ['svc:payment-api']",
                                references=("incident",), verdict=True, confidence=0.9))
    return ctx


def test_generates_multiple_candidate_actions():
    cands = generate_candidates("svc:cust-db")
    plans = {c.action.decision_goal for c in cands}
    names = {c.action.plan.split(":")[0] for c in cands}
    # ít nhất: restart, failover, drain, escalate, wait.
    assert {"restart", "failover", "drain", "escalate", "wait"} <= names
    assert all(c.action.state is ActionState.PLANNED for c in cands)  # chưa execute


def test_decide_picks_lowest_risk_resolving_action():
    ctx = _ctx_with_incident()
    decision = decide_recovery(ctx, failed_node="svc:cust-db")
    assert isinstance(decision, Decision)
    # restart = rủi ro nhỏ nhất mà giải quyết → được chọn.
    assert ctx.decisions and ctx.decisions[0] is decision
    chosen = ctx.actions[0]
    assert chosen.plan.startswith("restart")
    assert chosen.state is ActionState.PLANNED  # KHÔNG execute


def test_decision_consumes_findings_for_explainability():
    ctx = _ctx_with_incident()
    decision = decide_recovery(ctx, failed_node="svc:cust-db")
    # INV_EXPLAINABILITY: Decision truy về Finding nó dựa vào.
    assert any("DOWN" in c for c in decision.consumes)
    assert decision.goal == "restore_service:svc:cust-db"


def test_decision_is_fail_closed_requires_approval():
    ctx = _ctx_with_incident()
    decide_recovery(ctx, failed_node="svc:cust-db")
    # Mutation chưa được phép: Action ở PLANNED, decision đánh dấu cần approval.
    assert ctx.actions[0].state is ActionState.PLANNED
    assert ctx.recovery_confidence is not None
    assert 0.0 < ctx.recovery_confidence <= 1.0
    assert ctx.requires_approval is True


def test_decision_confidence_depends_on_diagnosis_confidence():
    # Chẩn đoán mơ hồ (0.35) → dù restart điểm cao, recovery_confidence bị kéo thấp.
    ctx = _ctx_with_incident()
    decide_recovery(ctx, failed_node="svc:cust-db", diagnosis_confidence=0.35)
    assert ctx.recovery_confidence < 0.35  # 0.6 × 0.35 ≈ 0.21 — không được "restart mù"

    # Chẩn đoán chắc chắn (0.9) → recovery_confidence cao.
    ctx2 = _ctx_with_incident()
    decide_recovery(ctx2, failed_node="svc:cust-db", diagnosis_confidence=0.9)
    assert ctx2.recovery_confidence > ctx.recovery_confidence


def test_no_resolving_action_when_only_safe_options(monkeypatch):
    # Nếu không có phương án nào "resolves" → chọn escalate (an toàn), confidence thấp.
    import aoip.decision as dmod
    ctx = _ctx_with_incident()
    monkeypatch.setattr(dmod, "_RESOLVING", frozenset())  # vô hiệu mọi phương án giải quyết
    decide_recovery(ctx, failed_node="svc:cust-db")
    assert ctx.actions[0].plan.startswith("escalate")
    assert ctx.recovery_confidence < 0.5
