# Integration tests

Slower tests that exercise multi-step flows. Unit runs typically **exclude** this directory:

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
```

## Stateful ReAct E2E (`test_e2e_autonomous_loop.py`)

Glassbox harness for [`run_agentic_mutate_plan`](../../src/workers/analyst_agentic_loop.py):

- **Two planner invocations** — production code returns immediately on a valid mutate JSON (mutate runs in **omni-executor**, not inside the planner). The test simulates the executor with `SimulatedClusterState.apply_patch_secret` between invocations A and B.
- **`SimulatedClusterState`** — in-memory Secret/credential state; readonly `k8s_describe_resource` output changes when `secret_patched` flips.
- **`SmartLLMStub`** — not a fixed `side_effect` list; decisions are driven by **phase** (`A` / `B`) + **round index**, with `decision_rule` recorded on each `chat()` for audit.
- **`ReActAuditTrail`** — structured steps (LLM rounds, readonly I/O, plan results, executor sim). Optional JSON file:

```bash
OMNI_E2E_AUDIT_JSON=/tmp/omni_e2e_audit.json \
  .venv/bin/python -m pytest tests/integration/test_e2e_autonomous_loop.py -v
```

Run all integration tests:

```bash
.venv/bin/python -m pytest tests/integration/ -q
# or
.venv/bin/python -m pytest -m integration -q
```
