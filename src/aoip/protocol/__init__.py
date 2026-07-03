"""Canonical command-delivery protocol vocabulary (ADR-002).

Nguồn chân lý DUY NHẤT cho state machine của mutating recovery command:

    QUEUED → DELIVERED → ACCEPTED → RUNNING → RECONCILING
           → COMPLETED | FAILED | ESCALATED | EXPIRED

Chỉ chứa pure constants + pure functions (stdlib-only) — KHÔNG transport, KHÔNG persistence.
Gateway (`gateway/routes/agent_runtime.py`), agent (`aoip.agent.delivery`), tests và scripts
đều import từ đây. Lua script trong Gateway không import được Python → contract test
(`tests/test_aoip_protocol_contract.py`) khẳng định bảng TERMINAL trong Lua source khớp
``TERMINAL_STATES`` ở đây; thêm state mới mà quên Lua sẽ fail test, không fail im lặng runtime.

Mọi thay đổi HÀNH VI protocol (thêm state, đổi transition) phải bump ``PROTOCOL_VERSION``
và kèm compatibility note trong ADR-002.
"""
from __future__ import annotations

PROTOCOL_VERSION = 1

ST_QUEUED = "QUEUED"
ST_DELIVERED = "DELIVERED"
ST_ACCEPTED = "ACCEPTED"
ST_RUNNING = "RUNNING"
ST_RECONCILING = "RECONCILING"
ST_COMPLETED = "COMPLETED"
ST_FAILED = "FAILED"
ST_ESCALATED = "ESCALATED"
ST_EXPIRED = "EXPIRED"

ALL_STATES = frozenset({
    ST_QUEUED, ST_DELIVERED, ST_ACCEPTED, ST_RUNNING, ST_RECONCILING,
    ST_COMPLETED, ST_FAILED, ST_ESCALATED, ST_EXPIRED,
})

TERMINAL_STATES = frozenset({ST_COMPLETED, ST_FAILED, ST_ESCALATED, ST_EXPIRED})

# Trạng thái agent được phép report qua /commands/progress (và heartbeat visibility).
PROGRESS_STATES = frozenset({ST_RUNNING, ST_RECONCILING})

# Thứ tự altitude cho non-terminal: không cho lùi (trừ redelivery về DELIVERED do
# visibility timeout — được phép, không coi là vi phạm).
PROGRESS_ORDER = {
    ST_QUEUED: 0, ST_DELIVERED: 1, ST_ACCEPTED: 2, ST_RUNNING: 3, ST_RECONCILING: 4,
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def is_legal_transition(current: str, target: str) -> bool:
    """Transition hợp lệ của delivery/runtime state machine.

    - Terminal là điểm hút: không rời terminal.
    - Vào terminal được từ mọi non-terminal.
    - Redelivery: non-terminal → DELIVERED luôn hợp lệ (visibility timeout).
    - Non-terminal khác: chỉ tiến altitude, không lùi.
    """
    if current not in ALL_STATES or target not in ALL_STATES:
        return False
    if current in TERMINAL_STATES:
        return False
    if target in TERMINAL_STATES:
        return True
    if target == ST_DELIVERED:
        return True  # redelivery
    return PROGRESS_ORDER[target] > PROGRESS_ORDER[current]
