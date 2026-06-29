"""Capability: Restart Kubernetes Deployment — vertical slice đầu tiên.

Toàn bộ là COMPOSITION của primitive qua Behavior Algebra — KHÔNG primitive mới
(INV_MINIMAL_PRIMITIVES). Phép thử closure: pipeline CTO yêu cầu
(Observe→Hypothesis→Decision→Approval→Execute→Observe→Verify→Assess→Capability↑)
viết được hoàn toàn bằng {Sequence, Choice} trên 8 verb.
"""
from __future__ import annotations

from aoip.algebra import Choice, Sequence
from aoip.context import ExecutionContext
from aoip.lifecycle import transition
from aoip.objects import ActionState
from aoip.primitives import (
    abort,
    assess,
    decide,
    escalate,
    execute,
    hypothesize,
    observe,
    plan,
    recover,
    validate,
    verify,
)


def _in_state(state: ActionState):
    return lambda ctx: ctx.action is not None and ctx.action.state == state


def _always(_ctx: ExecutionContext) -> bool:
    return True


async def _gate(ctx: ExecutionContext) -> None:
    """Approval gate (KHÔNG phải Execution primitive) — ghi quyết định vào action.result."""
    granted = await ctx.approval.approve(ctx.scope, ctx.action.plan)
    ctx.action = ctx.action.at(ctx.action.state, approved=granted)
    ctx.log("Approve", f"granted={granted}")


def _approved(ctx: ExecutionContext) -> bool:
    return bool(ctx.action and ctx.action.result.get("approved"))


async def _to_approved(ctx: ExecutionContext) -> None:
    ctx.action = transition(ctx.action, ActionState.APPROVED)


# Sau Execute: Choice(completed → Observe+Verify+Assess | failed → Recover+Escalate+...)
_execute_and_settle = Sequence(
    execute,
    Choice(
        (_in_state(ActionState.COMPLETED), Sequence(observe, verify, assess)),
        (_in_state(ActionState.FAILED), Sequence(recover, escalate, observe, verify, assess)),
    ),
)

# Pipeline đầy đủ (đúng thứ tự CTO yêu cầu).
restart_deployment = Sequence(
    observe,        # Observe cluster
    hypothesize,    # Generate hypothesis
    decide,         # Decision
    plan,           # Plan → Action(planned)
    validate,       # Validate → validated
    _gate,          # Approval (gate)
    Choice(
        (_approved, Sequence(_to_approved, _execute_and_settle)),
        (_always, abort),
    ),
)
