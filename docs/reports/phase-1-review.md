# Phase 1 Review - Mutate Channel Semantics

## Findings
- Channel semantics were previously permissive and allowed read-only tools in mutate flow.
- Planner reject taxonomy needed standard reason codes for SIEM consistency.

## Design Decisions
- Use capability taxonomy as source of truth; keep prefix policy as advisory evolution path.
- Fail-closed on executor side even if planner misbehaves.

## Trade-offs
- Stricter mutate allowlist may reduce short-term flexibility but removes false execution closure.

## Iteration Findings - Security Cleanup
- Historical commits still contain old DSN secrets (`gitleaks detect` history mode).
- Working tree secret gate passes after placeholder replacement + allowlist tightening.

## Iteration Design Decisions
- Keep CI secret gate on `--no-git` to block new leaks.
- Keep separate history audit target (`make secret-history-audit`) and require rotate + explicit approval before history rewrite.
