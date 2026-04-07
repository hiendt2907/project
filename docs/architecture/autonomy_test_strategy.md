# Autonomy Test Strategy

## Layers

- Unit: guardrails, serializers, deterministic helpers.
- Integration: closed-loop transitions with faked dependencies.
- E2E: trace-by-trace stage assertions on live cluster.

## Mandatory Scenarios

- happy path verified success
- execution failure with replan and retry
- policy deny / freeze / rate-limit
- timeout and partial dependency failures

## Pass Criteria

- ordered transitions present
- terminal state present
- security assertions hold