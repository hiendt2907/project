"""Decision Engine — Incident → Candidate Actions → Decision (EPIC Operate, 2).

Vì sao tồn tại: Recovery KHÔNG được nhận Incident trực tiếp. Phải có tầng Decision
ở giữa (ontology đã khóa: INV_DECISION_ACTION_SEPARATION — "có nên" ≠ "làm thế nào").
Khi sự cố xảy ra có NHIỀU phương án (restart/failover/drain/escalate/wait); agent
phải sinh ứng viên, chấm điểm, và RA QUYẾT ĐỊNH có giải trình — KHÔNG tự thực thi.

Bất biến tuân: INV_SMALL_BLAST_RADIUS (chọn phương án rủi ro nhỏ nhất đủ giải
quyết), INV_EXPLAINABILITY (Decision truy về Finding), INV_FAIL_CLOSED +
INV_HUMAN_ACCOUNTABILITY (mutation cần human approval — Action ở PLANNED, KHÔNG
enact). Tái dùng Decision/Action ontology — KHÔNG noun mới. ScoredAction là Derived
metadata quyết định (không persist).
"""
from __future__ import annotations

from dataclasses import dataclass

from aoip.objects import Action, ActionState, Decision

# Phương án "thật sự khắc phục" sự cố (vs escalate/wait chỉ an toàn, không sửa).
_RESOLVING = frozenset({"restart", "failover", "drain"})

# Playbook phục hồi cho một service node: (name, mô tả plan, risk 0..1).
# Mission là "khôi phục dịch vụ", KHÔNG phải "restart redis" — nhiều cách đạt mục tiêu.
_PLAYBOOK: tuple[tuple[str, str, float], ...] = (
    ("restart",  "restart: khởi động lại service tại chỗ", 0.30),
    ("drain",    "drain: rút traffic → restart → verify", 0.40),
    ("failover", "failover: promote replica + switch endpoint", 0.55),
    ("escalate", "escalate: page on-call (an toàn, không tự sửa)", 0.00),
    ("wait",     "wait: chờ 60s rồi probe lại (transient?)", 0.05),
)


@dataclass(frozen=True)
class ScoredAction:
    """Derived metadata quyết định (không persist): ứng viên + điểm."""

    action: Action
    risk: float
    resolves: bool
    confidence: float
    rationale: str


def generate_candidates(failed_node: str) -> list[ScoredAction]:
    """Sinh các phương án phục hồi (Action PLANNED) cho node hỏng, kèm điểm."""
    goal = f"restore_service:{failed_node}"
    cands: list[ScoredAction] = []
    for name, plan, risk in _PLAYBOOK:
        resolves = name in _RESOLVING
        # confidence: phương án giải quyết → cao, trừ theo rủi ro; an toàn-không-sửa → thấp.
        confidence = round((0.9 - risk) if resolves else 0.25, 3)
        cands.append(ScoredAction(
            action=Action(decision_goal=name, scope=goal, plan=plan, state=ActionState.PLANNED),
            risk=risk, resolves=resolves, confidence=confidence,
            rationale=f"{name}: resolves={resolves} risk={risk}",
        ))
    return cands


def decide_recovery(ctx, *, failed_node: str) -> Decision:
    """Chọn phương án phục hồi từ Finding sự cố → Decision (KHÔNG execute).

    Ưu tiên: phương án GIẢI QUYẾT có rủi ro nhỏ nhất (INV_SMALL_BLAST_RADIUS); nếu
    không có → escalate an toàn. Decision được 'justified' nhưng chưa 'issued' —
    mutation chờ human approval (fail-closed).
    """
    candidates = generate_candidates(failed_node)

    resolving = [c for c in candidates if c.resolves]
    pool = resolving or [c for c in candidates if c.action.decision_goal == "escalate"]
    # rủi ro nhỏ nhất trước; hoà thì confidence cao hơn.
    chosen = min(pool, key=lambda c: (c.risk, -c.confidence))

    # Decision consumes các Finding sự cố (explainability).
    consumed = tuple(f.claim for f in ctx.findings if f.verdict)
    decision = Decision(goal=f"restore_service:{failed_node}", scope=ctx.scope, consumes=consumed)

    ctx.decisions.append(decision)
    ctx.actions.append(chosen.action)  # PLANNED — chưa enact
    ctx.recovery_confidence = chosen.confidence
    ctx.requires_approval = True  # mọi mutation cần human (INV_HUMAN_ACCOUNTABILITY)
    ctx.log(
        "Decide",
        f"{len(candidates)} phương án → chọn '{chosen.action.decision_goal}' "
        f"(risk={chosen.risk} confidence={chosen.confidence}); chờ approval (fail-closed)",
    )
    return decision
