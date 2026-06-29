"""Action state-machine — enforce SEMANTIC_RULES Appendix A.1.

INV_LIFECYCLE_BEFORE_ALGORITHM: primitive chỉ được dịch theo transition hợp lệ.
Transition CẤM → ``IllegalTransition`` (ví dụ completed→executing).
"""
from __future__ import annotations

from aoip.objects import Action, ActionState, TERMINAL_ACTION_STATES

# Legal transitions (Appendix A.1)
_LEGAL: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PLANNED: frozenset({ActionState.VALIDATED, ActionState.ABORTED}),
    ActionState.VALIDATED: frozenset({ActionState.APPROVED, ActionState.ABORTED}),
    ActionState.APPROVED: frozenset({ActionState.EXECUTING, ActionState.ABORTED}),
    ActionState.EXECUTING: frozenset({ActionState.COMPLETED, ActionState.FAILED}),
    ActionState.FAILED: frozenset({ActionState.ROLLING_BACK}),
    ActionState.ROLLING_BACK: frozenset({ActionState.ROLLED_BACK}),
}


class IllegalTransition(RuntimeError):
    """Vi phạm lifecycle = bug (INV_LIFECYCLE_BEFORE_ALGORITHM)."""


def can_transition(frm: ActionState, to: ActionState) -> bool:
    return to in _LEGAL.get(frm, frozenset())


def transition(action: Action, to: ActionState, **result) -> Action:
    if not can_transition(action.state, to):
        raise IllegalTransition(f"{action.state.value} → {to.value} is illegal")
    return action.at(to, **result)


def is_terminal(action: Action) -> bool:
    return action.state in TERMINAL_ACTION_STATES
