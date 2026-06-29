"""Walking-skeleton runner — chạy capability end-to-end, in vòng phản hồi Capability.

    python -m aoip.runner

Chứng minh: pipeline chạy thật; sau mỗi lần thành công, CapabilityState (chiều E +
maturity) tăng — vòng Observe→…→Assess→Capability update đã đóng.
"""
from __future__ import annotations

import asyncio

from aoip.backends import AutoApprove, MockK8sBackend
from aoip.capabilities.restart_deployment import restart_deployment
from aoip.capability import CapabilityState
from aoip.context import ExecutionContext


async def run_once(capability_state: CapabilityState, *, fail: bool = False) -> ExecutionContext:
    ctx = ExecutionContext(
        scope="payment/web",
        backend=MockK8sBackend(fail_restart=fail),
        approval=AutoApprove(),
        capability=capability_state,
        namespace="payment",
        deployment="web",
    )
    await restart_deployment(ctx)
    return ctx


async def main() -> None:
    cap = CapabilityState(capability_id="restart_deployment", scope="payment/web")
    print(f"START  score={cap.score:.4f} E={cap.dimensions['E']} maturity={cap.maturity.value}")
    for i in range(1, 4):
        ctx = await run_once(cap)
        cap = ctx.capability
        print(f"\n── run {i} ──")
        for line in ctx.trace:
            print("  " + line)
        print(
            f"  RESULT action={ctx.action.state.value} "
            f"score={cap.score:.4f} maturity={cap.maturity.value}"
        )
    print(f"\nEND    score={cap.score:.4f} maturity={cap.maturity.value}")


if __name__ == "__main__":
    asyncio.run(main())
