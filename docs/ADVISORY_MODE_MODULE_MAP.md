# Advisory Mode Phase 5 — Module Dependency Map

## New Modules Created

### Schema & Types
- **`src/pkg/reasoning/analyst_advisory_schema.py`** (234 lines)
  - Pydantic models: AnalystAdvisory, VerificationStep, ProposedRemediationStep, ImpactForecast, ForecastTimeline
  - Enforces strict output shape for LLM + validation

### Forecasting & Evidence
- **`src/prober/temporal_evidence.py`** (159 lines)
  - `TemporalMetric`: rate-of-change calculation + linear extrapolation
  - `TemporalEvidenceBlock`: Aggregates metrics + current state + forecasts
  - Serializes to prompt-friendly Markdown blocks

### System Prompts
- **`src/workers/advisory_mode_system_prompt.py`** (217 lines)
  - `build_advisory_system_prompt()`: Complete LLM prompt for Advisory Mode
  - Infrastructure forecasting: linear extrapolation from rate data
  - Security forecasting: MITRE ATT&CK Kill Chain phases
  - Examples for OOMKilled + security escalation

### Analyst Handler
- **`src/workers/advisory_analyst_handler.py`** (138 lines)
  - `run_advisory_analyst()`: Async handler; calls LLM with advisory prompt
  - `_parse_advisory_json()`: Robust JSON extraction
  - Logs LLM traces + metrics

### Safety
- **`src/workers/advisory_mode_kill_switch.py`** (156 lines)
  - `AdvisoryModeKillSwitch`: Static class with hardcoded `OMNI_AUTO_EXECUTE_ENABLED = False`
  - `validate_execution_gate()`: Pre-execution check (ALWAYS blocks)
  - `trap_hallucinated_mutation()`: Async fallback; emits Telegram suggestion instead of executing
  - `validate_advisor_output()`: Post-generation check (scans for embedded mutations)

### Telegram Emitter
- **`src/workers/telegram_advisory_emitter.py`**
  - `render_advisory_to_telegram()`: Renders AnalystAdvisory to Markdown
  - `copy_advisory_for_telegram_if_mismatch()`: Telegram-only toned-down copy when SDK shows healthy pod but model escalates (CRAT keeps original)
  - `render_advisory_batch_to_telegram(..., evidence_text=None)`: Batch summary with `_e()` on dynamic lines; when `evidence_text` is set and summary is long, each per-advisory send uses the same sanitize helper as `evidence_consumer`
  - `_render_verdict_header()`, `_render_verification_steps()`, etc.: Component renders

---

## Integration Points (Existing Files to Modify)

### 1. Evidence Prober (`src/workers/diagnostic_evidence.py`)
**Changes:**
- Import `TemporalEvidenceBlock` + `fetch_temporal_evidence_for_batch()`
- Before calling LLM, fetch 1-hour historical metrics
- Inject temporal block into evidence narrative via `.to_prompt_block()`

**Why:** Gives LLM the rate-of-change data needed for forecasting

---

### 2. Evidence Consumer (`src/workers/evidence_consumer.py`)
**Changes:**
- Remove call to `run_agentic_mutate_plan()` (old ReAct planner)
- Add call to `run_advisory_analyst()` (new Advisory Analyst)
- Check `AdvisoryModeKillSwitch.validate_advisor_output()`
- Call `render_advisory_to_telegram()` if valid

**Why:** Routes evidence through Advisory pipeline instead of executor

---

### 3. Executor Entry Point (`src/pkg/executor/__init__.py` or executor module)
**Changes:**
- Add pre-execution gate:
  ```python
  allow, reason = AdvisoryModeKillSwitch.validate_execution_gate(tool_name, args, context)
  if not allow:
      return await AdvisoryModeKillSwitch.trap_hallucinated_mutation(...)
  ```
- Add assertion:
  ```python
  assert AdvisoryModeKillSwitch.OMNI_AUTO_EXECUTE_ENABLED, "Kill-switch active"
  ```

**Why:** Multi-layered defense; catches any mutation attempt

---

### 4. Settings (`src/settings.py` or `k8s/configmaps/`)
**Changes:**
- Add:
  ```python
  OMNI_AUTO_EXECUTE_ENABLED: bool = False
  OMNI_SIEM_SUGGEST_ONLY: bool = True
  OMNI_ADVISORY_MODE_ENABLED: bool = True
  OMNI_TEMPORAL_EVIDENCE_ENABLED: bool = True
  OMNI_TEMPORAL_EVIDENCE_MAX_AGE_MINUTES: int = 15
  ```

**Why:** Configuration foundation for Advisory Mode

---

### 5. Startup Validation (`src/pkg/env.py` or `main()`)
**Changes:**
- Call `validate_advisory_mode()`:
  ```python
  assert not AdvisoryModeKillSwitch.OMNI_AUTO_EXECUTE_ENABLED
  logger.info("Advisory Mode gate passed")
  ```

**Why:** Fail-fast if kill-switch is not properly initialized

---

### 6. Evidence Adapter (`src/services/evidence_adapter/worker.py`)
**Changes (optional but recommended):**
- Add `fetch_prometheus_rate()` method
- Fetch `rate(container_cpu_usage_seconds_total[5m])` over 1h
- Return `[(timestamp, value), ...]` list

**Why:** Supplies temporal metrics to Prober

---

## Data Flow Diagram

```
┌─────────────┐
│ Alert/Event │
└──────┬──────┘
       │ [payload with namespace, pod, labels]
       ▼
┌─────────────────────────┐
│ Evidence Prober         │
│ (diagnostic_evidence.py)│
│ + Temporal Fetcher      │
└──────┬──────────────────┘
       │ [evidence + [TEMPORAL_EVIDENCE] block]
       │ [rate data included if available]
       ▼
┌──────────────────────────┐
│ Advisory Analyst         │
│ (advisory_analyst_        │
│  handler.py)             │
│ Calls LLM with:          │
│ - System prompt (expert) │
│ - Evidence (facts)       │
└──────┬───────────────────┘
       │ [raw LLM response JSON]
       ▼
┌─────────────────────────┐
│ JSON Parser             │
│ _parse_advisory_json()  │
└──────┬──────────────────┘
       │ [parsed dict]
       ▼
┌─────────────────────────┐
│ Schema Validation       │
│ AnalystAdvisory(**dict) │
└──────┬──────────────────┘
       │ [AnalystAdvisory object]
       ▼
┌─────────────────────────────────┐
│ Kill-Switch Validation          │
│ (advisory_mode_kill_switch.py)  │
│ validate_advisor_output()       │
└──────┬────────┬──────────────────┘
       │ VALID  │ INVALID
       ▼        ▼
┌──────────────┐ ┌─────────────────────────────┐
│ Telegram     │ │ Trap + Emit "Bad Advisory"  │
│ Emitter      │ │ → Suggest corrected action  │
│ (render...) │ │ → Log error for review      │
└──────┬──────┘ └─────────────────────────────┘
       │
       ▼
┌──────────────────────┐
│ Telegram             │
│ Message with:        │
│ - Verdict (emoji)    │
│ - Root cause         │
│ - Verification steps │
│ - Remediation (adv.) │
│ - Forecast (chart)   │
└──────────────────────┘
       │
       ▼
    [Operator Reviews]
       │
       ├─→ "Approve" → Manual execution
       ├─→ "Modify" → Edit command + execute
       ├─→ "Skip" → Mark resolved
       └─→ "Defer" → Snooze + retry later
```

---

## Call Graph (Key Paths)

### Path 1: Evidence → Advisory → Telegram (Happy Path)
```
evidence_consumer.handle_inbound()
  └─→ run_advisory_analyst()
      ├─→ build_advisory_system_prompt() [system prompt]
      ├─→ ctx.llm.chat() [LLM call]
      └─→ _parse_advisory_json() [JSON parsing]
  └─→ validate_advisor_output() [kill-switch]
  └─→ render_advisory_to_telegram() [send to Telegram]
```

### Path 2: Mutation Attempt → Trapped (Fail-Safe Path)
```
executor.execute(tool_name, args, context)
  └─→ validate_execution_gate()
      └─→ ✓ Block → trap_hallucinated_mutation()
          ├─→ Send Telegram: "Suggested Action (NOT EXECUTED)"
          └─→ Return advisory message
```

### Path 3: Prober → Temporal Data → Evidence Block
```
diagnostic_evidence.reason_diagnostic_evidence_only()
  └─→ fetch_temporal_evidence_for_batch()
      ├─→ prometheus.query_range(metric, -3600, 60)
      └─→ TemporalEvidenceBlock.add_metric()
  └─→ temporal_block.to_prompt_block()
      └─→ Inject [TEMPORAL_EVIDENCE] into evidence_text
```

---

## Configuration Chain

```
k8s/configmaps/omni-worker-config.yaml
  │
  ├─→ OMNI_AUTO_EXECUTE_ENABLED: "false"
  │   └─→ AdvisoryModeKillSwitch.OMNI_AUTO_EXECUTE_ENABLED
  │       └─→ Used in validate_execution_gate()
  │
  ├─→ OMNI_TEMPORAL_EVIDENCE_ENABLED: "true"
  │   └─→ diagnostic_evidence.py checks before fetching metrics
  │
  ├─→ OMNI_ADVISORY_MODE_ENABLED: "true"
  │   └─→ evidence_consumer.py decides advisory vs planner
  │
  └─→ OMNI_TEMPORAL_EVIDENCE_MAX_AGE_MINUTES: "15"
      └─→ Used in advisory_analyst_handler to reject stale data
```

---

## Test Coverage Map

| Module | Test File | Coverage |
|--------|-----------|----------|
| `analyst_advisory_schema.py` | `tests/test_advisory_schema.py` | Pydantic validation |
| `temporal_evidence.py` | `tests/test_temporal_metrics.py` | Rate calc + extrapolation |
| `advisory_mode_system_prompt.py` | `tests/test_advisory_prompt.py` | Prompt consistency |
| `advisory_analyst_handler.py` | `tests/test_advisory_analyst.py` | LLM call + parsing |
| `advisory_mode_kill_switch.py` | `tests/test_kill_switch.py` | Execution gate + trap |
| `telegram_advisory_emitter.py` | `tests/test_telegram_render.py` | Markdown output |

---

## Deployment Checklist

- [ ] All new modules added to git
- [ ] Imports updated in existing files (evidence_consumer, executor, etc.)
- [ ] Settings added to `k8s/configmaps/omni-worker-config.yaml`
- [ ] Startup validation added to `main()` or `app.on_event("startup")`
- [ ] Tests passing: `pytest tests/ -k advisory`
- [ ] Linting: `black src/workers/advisory* && ruff check src/workers/advisory*`
- [ ] Type checking: `mypy src/workers/advisory*`
- [ ] Docker image built: `make docker-worker`
- [ ] Deployed to lab: `kubectl apply -f k8s/deployments/omni-worker-lab.yaml`
- [ ] Smoke test: Send test alert → verify advisory in Telegram
- [ ] Monitor logs: `kubectl logs -l app=omni-worker | grep advisory`
- [ ] Check kill-switch: `kubectl logs -l app=omni-worker | grep "kill_switch_blocked" | wc -l` (should be 0)

---

## Future Enhancements

1. **ML Tuning:** Track forecast vs actual; refine rate-of-change thresholds
2. **Playbook Integration:** Match advisories to pre-approved playbooks (low-risk auto-exec)
3. **Incident Correlation:** Group related advisories into single incident summary
4. **Forecast History:** Store all forecasts; measure accuracy over time
5. **Cost Analysis:** Predict cost impact of proposed remediation (e.g., scaling up)
