"""Mission Runtime — điều phối các capability thay vì gọi tuần tự từng feature.

Vì sao tồn tại (pivot chiến lược): AOIP bán CAPABILITY, không bán Knowledge. Một
capability (understand_host, understand_service, understand_tenant, investigate_
incident) phải được thực thi như một MISSION = composition của primitive/runtime
đã có, đánh giá bằng Definition-of-Done, đo bằng Mission Completion (% hiểu) — đơn
vị giá trị khách hàng thực sự thấy.

Mission ĐÃ là Runtime noun (META_MODEL §Runtime; lifecycle trong SEMANTIC_RULES) —
đây là IMPLEMENT, KHÔNG noun mới. Tuân INV_LIFECYCLE_BEFORE_ALGORITHM: lifecycle
khai báo tường minh trước khi chạy thuật toán. DoD/completion là field+derived,
runtime; MissionStore chỉ lưu projection vận hành tenant-scoped để portal theo dõi,
không lưu evidence hay dữ liệu khách hàng.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Awaitable, Callable

# ── Lifecycle (SEMANTIC_RULES §Mission) ──────────────────────────────────────
class MissionState(str, Enum):
    CREATED = "created"
    PLANNED = "planned"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"
    ARCHIVED = "archived"


# CREATED→PLANNED→ASSIGNED→IN_PROGRESS⇄BLOCKED→COMPLETED→ARCHIVED. CẤM skip ASSIGNED,
# CẤM COMPLETED→IN_PROGRESS. Terminal = ARCHIVED.
_LEGAL_TRANSITIONS: dict[MissionState, frozenset[MissionState]] = {
    MissionState.CREATED: frozenset({MissionState.PLANNED}),
    MissionState.PLANNED: frozenset({MissionState.ASSIGNED}),
    MissionState.ASSIGNED: frozenset({MissionState.IN_PROGRESS}),
    MissionState.IN_PROGRESS: frozenset(
        {MissionState.BLOCKED, MissionState.COMPLETED, MissionState.FAILED, MissionState.ABANDONED}
    ),
    MissionState.BLOCKED: frozenset(
        {MissionState.IN_PROGRESS, MissionState.COMPLETED, MissionState.FAILED, MissionState.ABANDONED}
    ),
    MissionState.COMPLETED: frozenset({MissionState.ARCHIVED}),
    MissionState.FAILED: frozenset({MissionState.ARCHIVED}),
    MissionState.ABANDONED: frozenset({MissionState.ARCHIVED}),
    MissionState.ARCHIVED: frozenset(),
}


@dataclass(frozen=True)
class Mission:
    """Runtime object bất biến; chuyển state bằng ``to`` (kiểm tra lifecycle)."""

    mission_id: str
    goal: str
    scope: str
    state: MissionState = MissionState.CREATED
    completion: float = 0.0  # Derived: % DoD đạt (Mission Completion Rate)
    dod_passed: tuple[str, ...] = ()
    dod_failed: tuple[str, ...] = ()
    parent_mission_id: str | None = None

    def to(self, state: MissionState, **changes) -> "Mission":
        if state not in _LEGAL_TRANSITIONS[self.state]:
            raise ValueError(
                f"Mission transition bất hợp lệ: {self.state.value} → {state.value}"
            )
        return replace(self, state=state, **changes)


# Một bước Mission là async fn mutate context dùng chung (Working Memory).
MissionStep = Callable[[object], Awaitable[None]]
# DoD check: tên + vị từ trên context cuối → đạt/không.
DoDCheck = tuple[str, Callable[[object], bool]]


async def run_mission(
    mission: Mission,
    ctx: object,
    *,
    plan: list[MissionStep],
    dod: list[DoDCheck],
) -> Mission:
    """Thực thi plan (composition) rồi chấm Definition-of-Done → Mission Completion.

    Pass hết DoD → COMPLETED; còn check fail (vd câu hỏi tồn đọng) → BLOCKED (chờ
    người hoặc bằng chứng thêm). Lỗi runtime → FAILED. Lifecycle ép tường minh.
    """
    mission = mission.to(MissionState.PLANNED).to(MissionState.ASSIGNED).to(
        MissionState.IN_PROGRESS
    )
    try:
        for step in plan:
            await step(ctx)
    except Exception:
        return mission.to(MissionState.FAILED, completion=0.0)

    passed = tuple(name for name, check in dod if check(ctx))
    failed = tuple(name for name, check in dod if not check(ctx))
    completion = 1.0 if not dod else len(passed) / len(dod)
    final = MissionState.COMPLETED if not failed else MissionState.BLOCKED
    return mission.to(final, completion=completion, dod_passed=passed, dod_failed=failed)


def aggregate_completion(subs: list[Mission]) -> float:
    """Mission cha (vd tenant) = trung bình completion của sub-Mission."""
    if not subs:
        return 0.0
    return sum(s.completion for s in subs) / len(subs)
