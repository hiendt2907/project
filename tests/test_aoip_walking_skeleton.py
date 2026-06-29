"""Walking-skeleton tests — runtime ép buộc framework (lộ trình CTO).

Bài kiểm tra thật: 1 capability chạy end-to-end qua primitive + algebra; lifecycle
hợp lệ; vòng Assess đóng (Capability tăng/giảm theo evidence). Không noun mới.
"""
from __future__ import annotations

import pytest

from aoip.backends import AutoApprove, MockK8sBackend
from aoip.capabilities.restart_deployment import restart_deployment
from aoip.capability import CapabilityState, Maturity
from aoip.context import ExecutionContext
from aoip.lifecycle import IllegalTransition, transition
from aoip.objects import Action, ActionState
from aoip.runner import run_once


def _fresh_cap() -> CapabilityState:
    return CapabilityState(capability_id="restart_deployment", scope="payment/web")


async def _run(*, fail: bool, cap: CapabilityState) -> ExecutionContext:
    ctx = ExecutionContext(
        scope="payment/web",
        backend=MockK8sBackend(fail_restart=fail),
        approval=AutoApprove(),
        capability=cap,
        namespace="payment",
        deployment="web",
    )
    await restart_deployment(ctx)
    return ctx


async def test_happy_path_completes_and_raises_capability():
    ctx = await _run(fail=False, cap=_fresh_cap())

    assert ctx.action.state is ActionState.COMPLETED
    assert ctx.findings[-1].verdict is True
    # Assess đóng vòng: E từ 0 → 1, score = Π > 0.
    assert ctx.capability.dimensions["E"] == 1.0
    assert ctx.capability.score == pytest.approx(1.0)


async def test_failure_path_recovers_and_lowers_capability():
    ctx = await _run(fail=True, cap=_fresh_cap())

    # Execute fail → Recover → action rolled_back.
    assert ctx.action.state is ActionState.ROLLED_BACK
    assert ctx.findings[-1].verdict is False
    # Outcome xấu → E = 0 → score = 0 (INV_CAPABILITY_IS_PRODUCT).
    assert ctx.capability.dimensions["E"] == 0.0
    assert ctx.capability.score == 0.0


async def test_capability_maturity_grows_with_evidence():
    cap = _fresh_cap()
    assert cap.maturity is Maturity.NASCENT
    for _ in range(3):
        ctx = await run_once(cap)
        cap = ctx.capability
    assert cap.maturity is Maturity.PROVEN


async def test_mixed_evidence_gives_partial_execution_score():
    cap = _fresh_cap()
    cap = (await _run(fail=False, cap=cap)).capability  # success
    cap = (await _run(fail=True, cap=cap)).capability    # fail
    # 1/2 thành công → E = 0.5.
    assert cap.dimensions["E"] == pytest.approx(0.5)


async def test_illegal_lifecycle_transition_is_blocked():
    # INV_LIFECYCLE_BEFORE_ALGORITHM: completed → executing là bất hợp lệ.
    a = Action(decision_goal="x", scope="s", plan="p", state=ActionState.COMPLETED)
    with pytest.raises(IllegalTransition):
        transition(a, ActionState.EXECUTING)


async def test_rejected_approval_aborts_without_execute():
    class _Deny:
        async def approve(self, scope: str, plan: str) -> bool:
            return False

    ctx = ExecutionContext(
        scope="payment/web",
        backend=MockK8sBackend(),
        approval=_Deny(),
        capability=_fresh_cap(),
        namespace="payment",
        deployment="web",
    )
    await restart_deployment(ctx)
    assert ctx.action.state is ActionState.ABORTED
    assert ctx.findings == []  # không Execute, không Verify
