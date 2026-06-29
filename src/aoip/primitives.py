"""Tám Execution primitive (EXECUTION_MODEL §1) + helper Cognitive/Operating.

Mỗi primitive dịch đúng một transition lifecycle (Appendix A.1) qua
``lifecycle.transition`` — vi phạm = IllegalTransition. Assess là composition
(Observe→Verify→recompute), KHÔNG primitive (ASSESSMENT.md).
"""
from __future__ import annotations

from aoip.capability import assess as _assess_capability
from aoip.context import ExecutionContext
from aoip.lifecycle import transition
from aoip.objects import (
    Action,
    ActionState,
    Decision,
    Finding,
    Hypothesis,
    Observation,
)


# ── Cognitive / Operating helpers (KHÔNG phải Execution primitive) ────────────
async def hypothesize(ctx: ExecutionContext) -> None:
    obs = ctx.observations[-1]
    unhealthy = obs.data.get("unhealthy_pods", 0) > 0
    h = Hypothesis(
        claim="deployment unhealthy do pod crash/stale → rollout restart khắc phục",
        predicted_evidence=("unhealthy_pods == 0 sau restart",),
        prior=0.7 if unhealthy else 0.05,
        origin="SYSTEM_MODEL",
    )
    ctx.hypotheses.append(h)
    ctx.log("Hypothesize", f"{h.claim} (prior={h.prior})")


async def decide(ctx: ExecutionContext) -> None:
    h = ctx.hypotheses[-1]
    # Decision consumes Finding/observation; ở skeleton consume hypothesis-claim.
    ctx.decision = Decision(
        goal="restart_deployment", scope=ctx.scope, consumes=(h.claim,)
    )
    ctx.log("Decide", f"goal=restart_deployment scope={ctx.scope}")


# ── 8 Execution primitives ────────────────────────────────────────────────────
async def observe(ctx: ExecutionContext) -> None:
    status = await ctx.backend.rollout_status(ctx.namespace, ctx.deployment)
    obs = Observation(source="k8s.rollout_status", scope=ctx.scope, data=status)
    ctx.observations.append(obs)
    ctx.log("Observe", str(status))


async def plan(ctx: ExecutionContext) -> None:
    assert ctx.decision is not None, "Plan requires a Decision"
    ctx.action = Action(
        decision_goal=ctx.decision.goal,
        scope=ctx.scope,
        plan=f"kubectl rollout restart deploy/{ctx.deployment} -n {ctx.namespace}",
    )
    ctx.log("Plan", ctx.action.plan)


async def validate(ctx: ExecutionContext) -> None:
    # Capability đủ? (E dimension không cần cho restart; check decision + lifecycle)
    ctx.action = transition(ctx.action, ActionState.VALIDATED)
    ctx.log("Validate", "decision hợp lệ, scope khớp, lifecycle ok")


async def execute(ctx: ExecutionContext) -> None:
    ctx.action = transition(ctx.action, ActionState.EXECUTING)
    result = await ctx.backend.rollout_restart(ctx.namespace, ctx.deployment)
    if result.get("ok"):
        ctx.action = transition(ctx.action, ActionState.COMPLETED, **result)
        ctx.log("Execute", f"completed: {result}")
    else:
        ctx.action = transition(ctx.action, ActionState.FAILED, **result)
        ctx.log("Execute", f"failed: {result}")


async def verify(ctx: ExecutionContext) -> None:
    obs = ctx.observations[-1]
    ok = obs.data.get("unhealthy_pods", 1) == 0 and obs.data.get("ready", False)
    finding = Finding(
        claim="deployment healthy sau remediation",
        references=(obs.source,),
        verdict=ok,
        confidence=0.9 if ok else 0.6,
    )
    ctx.findings.append(finding)
    ctx.log("Verify", f"verdict={ok}")


async def recover(ctx: ExecutionContext) -> None:
    ctx.action = transition(ctx.action, ActionState.ROLLING_BACK)
    ctx.action = transition(ctx.action, ActionState.ROLLED_BACK)
    ctx.log("Recover", "rolled back failed action")


async def escalate(ctx: ExecutionContext) -> None:
    ctx.log("Escalate", "vượt quyền/bế tắc → HITL (skeleton: ghi nhận)")


async def abort(ctx: ExecutionContext) -> None:
    if ctx.action is not None:
        ctx.action = transition(ctx.action, ActionState.ABORTED)
    ctx.log("Abort", "dừng an toàn")


# ── Assess (composition: Observe→Verify→recompute) ────────────────────────────
async def assess(ctx: ExecutionContext) -> None:
    """Đóng vòng phản hồi: outcome (Finding) → cập nhật CapabilityState."""
    outcome = bool(ctx.findings and ctx.findings[-1].verdict)
    ctx.capability = _assess_capability(ctx.capability, outcome)
    ctx.log(
        "Assess",
        f"outcome={outcome} → E={ctx.capability.dimensions['E']} "
        f"score={ctx.capability.score:.4f} maturity={ctx.capability.maturity.value}",
    )
