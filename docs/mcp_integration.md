# MCP integration (ADR — pilot)

## Context

Omni-worker tools today resolve through in-process `TOOL_REGISTRY` (`workers/tools.py`, `workers/tool_registry.py`). Proactive auto-remediation restricts mutations to `PROACTIVE_MUTATE_TOOLS` intersected with allowlists (`workers/proactive_guardrails.py`, `workers/proactive_tool_policy.py`).

## Decision

1. **Default plane:** Keep tool execution in-process via `TOOL_REGISTRY` for correctness, RBAC, leases, and audit hooks already wired in `proactive_observer` / handlers.
2. **MCP pilot:** Optional MCP is **read-only** only: diagnostics, metrics, log tail summaries — never a path for proactive mutations until a later ADR explicitly extends it.
3. **Proactive mutate:** Remains **in-process** only (`PROACTIVE_MUTATE_TOOLS`). MCP must not become an alternate execution plane for rollout/restart class tools in the proactive path.
4. **Configuration:** `OMNI_MCP_ENABLED` defaults off; `OMNI_MCP_SERVER_URL` is optional and unused until a client is implemented.

## Consequences

- No new network dependency in the default deployment.
- Future MCP servers can expose overlapping tool names; the worker must map names explicitly and refuse unknown or mutate-class tools over MCP during pilot.
- See `workers/tool_backend.py` for a `ToolBackend` protocol and `RegistryToolBackend` delegating to `TOOL_REGISTRY` (behavior-preserving stub).

## References

- Proactive state machine: [`docs/proactive_state_machine.md`](proactive_state_machine.md)
- Tool policy constants: `workers/proactive_tool_policy.py`
