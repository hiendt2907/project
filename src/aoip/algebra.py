"""Behavior Algebra — 5 toán tử composition (SEMANTIC_RULES Appendix B).

Mọi algorithm = primitive kết hợp CHỈ qua các toán tử này (INV_BEHAVIOR_ALGEBRA).
Walking skeleton dùng Sequence / Choice / Loop; Parallel / Interrupt khai báo sẵn
seam (runtime sẽ ép khi cần — chưa dùng thì chưa hiện thực đầy đủ).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from aoip.context import ExecutionContext

Step = Callable[[ExecutionContext], Awaitable[None]]
Guard = Callable[[ExecutionContext], bool]


def Sequence(*steps: Step) -> Step:
    async def run(ctx: ExecutionContext) -> None:
        for step in steps:
            await step(ctx)

    return run


def Choice(*branches: tuple[Guard, Step]) -> Step:
    """Rẽ nhánh theo guard loại trừ; nhánh đầu thỏa guard sẽ chạy."""

    async def run(ctx: ExecutionContext) -> None:
        for guard, step in branches:
            if guard(ctx):
                await step(ctx)
                return

    return run


def Loop(body: Step, *, until: Guard, max_iter: int = 10) -> Step:
    """Lặp body tới `until` hoặc bound (INV_FAIL_CLOSED: luôn có bound)."""

    async def run(ctx: ExecutionContext) -> None:
        for _ in range(max_iter):
            if until(ctx):
                return
            await body(ctx)
        # bound chạm: dừng an toàn, để guard kế quyết định (fail-closed)

    return run
