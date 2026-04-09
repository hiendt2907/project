# Diagnostic Policy & Agentic Reasoning Framework

**Goal:** “Luật giữ khung — LLM giữ hồn.” Omni reasons about novel failures without hardcoding every branch, while **non-negotiable SRE invariants** stay in code (Python), not in model weights.

## 1. Invariant layer (hardware — LLM cannot override)

| Invariant ID | Rule | On violation |
|----------------|------|----------------|
| `INV_NO_RESTART_ON_BROKEN_SPEC` | Do not `k8s_rollout_restart` when SDK/events show missing ConfigMap/Secret/mount dependency. | REJECT plan; route to `SUGGEST_FIX` / `SUGGEST_FIX_SOURCE` with verdict. |
| `INV_READ_BEFORE_MUTATE` | For hard-fault evidence, at least one read-only discovery step (tool call **or** equivalent batch evidence) before mutate. | DEFER mutate; require discovery / richer evidence. |
| `INV_NAMESPACE_ISOLATION` | All diagnose/repair targets must stay in `autonomous_allowed_namespaces`. | HARD_BLOCK; log security signal. |

Implementation: [`src/pkg/reasoning/diagnostic_policy.py`](../../src/pkg/reasoning/diagnostic_policy.py).

## 2. Discovery tools (sandbox “eyes”)

Spec names map to executor/registry tools (read-only):

| Spec name | Registry / behavior |
|-----------|---------------------|
| `k8s_inspect_resource` | `k8s_describe_resource`, `inspect_pod_deep` / `inspect_pod_details` |
| `loki_pattern_analysis` | Loki query helpers (e.g. log surge bypass) — optional / phased |
| `prom_vector_context` | `query_prometheus_metrics` / Prom instant — optional / phased |

Gaps are documented in code via `DISCOVERY_TOOL_ALIASES` and settings.

## 3. ReAct prompt frame (Observation → Hypothesis → Verification → Action)

System guidance (summary):

1. **Observation:** list concrete facts and contradictions from the Fact Table.
2. **Hypothesis:** at least two plausible causes.
3. **Verification:** call read-only Discovery tools to eliminate hypotheses.
4. **Final action:** mutate **only** after verification and **only** if invariants pass.

Prompt wiring: [`src/workers/analyst_agentic_loop.py`](../../src/workers/analyst_agentic_loop.py) when `OMNI_DIAGNOSTIC_REACT_ENABLED=1`.

## 4. Blind path (matrix ambiguous)

When the incident matrix does not pin a row, optional `blind_lane_hint` (e.g. from LLM or annotation) may select `proof_lane` if it is one of `resource` | `state` | `app_log`. Code still runs invariant checks.

## 5. UX: Reasoning report JSON

Suggested payload fields on `SUGGEST_REMEDIATION` / diagnostic audit (see [`src/workers/omni_actions_remediation.py`](../../src/workers/omni_actions_remediation.py)):

```json
{
  "verdict": "SUGGEST_FIX",
  "lane": "state",
  "thought_process": [
    "Detected CreateContainerConfigError for CM 'nginx-cfg'.",
    "Verified: ConfigMap 'nginx-cfg' is missing in namespace 'staging'.",
    "Action 'restart' rejected by INV_NO_RESTART_ON_BROKEN_SPEC.",
    "Proposed: Create ConfigMap using provided YAML."
  ],
  "invariant_id": "INV_NO_RESTART_ON_BROKEN_SPEC"
}
```

`verdict` examples: `SUGGEST_FIX`, `SUGGEST_FIX_SOURCE`, `EXECUTE_OK`, `DEFERRED`.

## 6. References

- Proof lanes: [`docs/reports/incident-evidence-three-lanes.md`](incident-evidence-three-lanes.md)
- Matrix: [`config/incident_training_matrix.yaml`](../../config/incident_training_matrix.yaml)
