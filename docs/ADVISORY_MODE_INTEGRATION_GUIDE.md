# Advisory Mode Phase 5 — Integration Guide

## Overview

Advisory Mode replaces the ReAct planner + executor with a structured **Analyst → Forecast → Telegram** pipeline.

**Flow:**
```
Evidence → Temporal Prober → Advisory Analyst → Kill-Switch Trap → Telegram Emitter
```

## Step 1: Wire Temporal Prober into diagnostic_evidence.py

**File:** `src/workers/diagnostic_evidence.py`

**Change:** Before calling `reason_diagnostic_evidence_only()`, fetch temporal metrics.

```python
# ADD TO reason_diagnostic_evidence_only():
from prober.temporal_evidence import TemporalEvidenceBlock

async def reason_diagnostic_evidence_only(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
) -> str:
    # ... existing code ...
    
    # NEW: Fetch temporal evidence (Prometheus rate, pod metrics history)
    temporal_block = await fetch_temporal_evidence_for_batch(
        ctx, 
        batch=payload.get("batched_probes", []),
        namespace=payload.get("namespace", ""),
        deployment=payload.get("deployment", ""),
    )
    
    # Inject temporal block into evidence narrative
    if temporal_block:
        working_text = f"{working_text}\n\n{temporal_block.to_prompt_block()}"
    
    # ... rest of existing code ...
```

**New helper function:**
```python
async def fetch_temporal_evidence_for_batch(
    ctx: WorkerHandlerContext,
    batch: list[dict[str, Any]],
    namespace: str,
    deployment: str,
) -> TemporalEvidenceBlock | None:
    """Fetch 1-hour historical metric window from Prometheus."""
    if not namespace or not deployment:
        return None
    
    block = TemporalEvidenceBlock("prometheus_history", namespace, "", deployment)
    
    # Example: fetch CPU rate over last 1 hour
    try:
        cpu_history = await ctx.prometheus.query_range(
            'rate(container_cpu_usage_seconds_total[5m])',
            start=-3600,  # 1 hour ago
            step=60,  # 1 datapoint per minute
        )
        if cpu_history:
            block.add_metric("cpu_percent", cpu_history)  # [(timestamp, value), ...]
    except Exception as e:
        logger.debug("temporal_fetch_cpu err=%s", e)
    
    block.set_current_state({"deployment": deployment, "namespace": namespace})
    return block
```

---

## Step 2: Replace ReAct Planner with Advisory Analyst

**File:** `src/workers/evidence_consumer.py` (or wherever `run_agentic_mutate_plan()` is called)

**Remove this:**
```python
# OLD CODE (to remove):
plan = await run_agentic_mutate_plan(
    ctx,
    trace=trace,
    sanitized_text=sanitized_text,
    batch=batch,
    max_steps=5,
)
if plan and plan.get("tool_name"):
    await emit_execute_mutate(ctx, plan)  # ← ALWAYS BLOCKED NOW
```

**Replace with this:**
```python
# NEW CODE:
from workers.advisory_analyst_handler import run_advisory_analyst
from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

# Run Advisory Analyst (read-only)
advisory = await run_advisory_analyst(
    ctx,
    payload=payload,
    trace=trace,
    evidence_text=sanitized_text,  # Includes temporal data now
)

if advisory:
    # Validate kill-switch
    valid, reason = AdvisoryModeKillSwitch.validate_advisor_output(advisory.model_dump())
    if not valid:
        logger.error("kill_switch_validation_failed reason=%s", reason)
        advisory = None  # Don't send invalid advisory
    else:
        # Send to Telegram
        if ctx.telegram and payload.get("chat_id"):
            await render_advisory_to_telegram(
                ctx, 
                advisory, 
                int(payload["chat_id"])
            )
```

---

## Step 3: Add Kill-Switch to Executor Entry Point

**File:** `src/pkg/executor/__init__.py` (or executor entrypoint)

**Add validation gate:**
```python
from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

async def execute_mutation(
    ctx: Any,
    tool_name: str,
    args: dict[str, Any],
    trace: str,
    context: str = "planner",
) -> dict[str, Any]:
    """Execute a mutation (read-only in Advisory Mode)."""
    
    # Kill-switch check (CRITICAL)
    allow, reason = AdvisoryModeKillSwitch.validate_execution_gate(
        tool_name, args, context
    )
    if not allow:
        # Trap and emit advisory message
        msg = await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name, args, ctx, trace
        )
        return {"error": msg, "executed": False}
    
    # If somehow we get here, proceed (shouldn't happen in Advisory Mode)
    # ... rest of executor logic ...
```

---

## Step 4: Wire Telegram Emitter into evidence_consumer

**File:** `src/workers/evidence_consumer.py`

**Add import:**
```python
from workers.telegram_advisory_emitter import (
    copy_advisory_for_telegram_if_mismatch,
    render_advisory_to_telegram,
    render_advisory_batch_to_telegram,
)
```

**Example: Send advisory on evidence_consumer message:**
```python
# In message handler for inbound evidence:
async def handle_evidence_inbound(payload: dict[str, Any], ctx: WorkerHandlerContext):
    advisory = await run_advisory_analyst(...)
    if advisory and ctx.telegram:
        # After CRAT write_audit_block(original advisory): Telegram-only clone when needed.
        tg_advisory = copy_advisory_for_telegram_if_mismatch(advisory, sanitized_evidence_text)
        await render_advisory_to_telegram(
            ctx,
            tg_advisory,
            chat_id=int(payload["chat_id"]),
        )
```

**Batch render:** pass the same evidence blob used for the analyst prompt when you want per-advisory sanitize on the long path (summary + individual messages):

```python
await render_advisory_batch_to_telegram(
    ctx, advisories, chat_id, batch_summary="...", evidence_text=sanitized_evidence_text
)
```

---

## Step 5: Update Settings (CRITICAL)

**File:** `src/settings.py` or `k8s/configmaps/omni-worker-config.yaml`

**Add:**
```python
# Advisory Mode hardcoded defaults
OMNI_AUTO_EXECUTE_ENABLED: bool = False  # Always False in Advisory Mode
OMNI_SIEM_SUGGEST_ONLY: bool = True      # Suggestions, no auto-execution
OMNI_ADVISORY_MODE_ENABLED: bool = True  # Enable new advisory analyst
OMNI_TEMPORAL_EVIDENCE_ENABLED: bool = True  # Fetch rate-of-change data
OMNI_TEMPORAL_EVIDENCE_MAX_AGE_MINUTES: int = 15  # Reject if older
```

**Or in Kubernetes ConfigMap:**
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: omni-worker-config
data:
  OMNI_AUTO_EXECUTE_ENABLED: "false"
  OMNI_SIEM_SUGGEST_ONLY: "true"
  OMNI_ADVISORY_MODE_ENABLED: "true"
  OMNI_TEMPORAL_EVIDENCE_ENABLED: "true"
  OMNI_TEMPORAL_EVIDENCE_MAX_AGE_MINUTES: "15"
```

---

## Step 6: Add Prometheus Queries for Temporal Data

**File:** `src/services/evidence_adapter/worker.py` (if using evidence adapter)

**Add temporal query method:**
```python
async def fetch_prometheus_rate(
    self,
    metric: str,
    hours: int = 1,
) -> list[tuple[float, float]]:
    """
    Fetch rate-of-change metric from Prometheus.
    
    Returns:
        [(timestamp_seconds, value), ...] sorted by timestamp
    """
    if not self.prometheus:
        return []
    
    try:
        # Example: rate(metric[5m]) over last N hours
        result = await self.prometheus.query_range(
            f'rate({metric}[5m])',
            start=time.time() - (hours * 3600),
            step=60,  # 1 datapoint per minute
        )
        if result and isinstance(result, list):
            return [(float(t), float(v)) for t, v in result if v is not None]
    except Exception as e:
        logger.debug("fetch_prometheus_rate err=%s", e)
    
    return []
```

---

## Step 7: Environment Validation

**File:** `src/pkg/env.py` or startup validation

**Add advisory mode gate:**
```python
def validate_advisory_mode():
    """Ensure Advisory Mode is properly initialized."""
    from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch
    
    assert not AdvisoryModeKillSwitch.OMNI_AUTO_EXECUTE_ENABLED, (
        "CRITICAL: OMNI_AUTO_EXECUTE_ENABLED must be False in Advisory Mode. "
        "Check k8s ConfigMap and environment."
    )
    
    logger.info(
        "event=advisory_mode_gate_passed "
        "auto_execute=%s suggest_only=%s",
        AdvisoryModeKillSwitch.OMNI_AUTO_EXECUTE_ENABLED,
        AdvisoryModeKillSwitch.OMNI_SIEM_SUGGEST_ONLY,
    )
```

**Call during startup:**
```python
# In main() or app startup:
validate_advisory_mode()
```

---

## Testing Checklist

- [ ] **Unit:** Parse AnalystAdvisory from LLM response; validate schema
- [ ] **Unit:** Kill-switch blocks mutations; trap correctly
- [ ] **Unit:** Telegram render produces valid Markdown (test emoji escaping)
- [ ] **Integration:** Advisory analyst outputs valid JSON for sample evidence
- [ ] **Integration:** Temporal prober fetches Prometheus data (test with mock Prometheus)
- [ ] **Integration:** Kill-switch assertion at executor entry point
- [ ] **Integration:** Telegram send succeeds; fallback to Redis on failure
- [ ] **E2E:** Evidence → Advisory → Telegram end-to-end (lab environment)
- [ ] **E2E:** Verify no mutations are executed (grep logs for executor.execute)
- [ ] **Load:** Send 100 advisories in batch; verify Telegram throughput

---

## Deployment Steps

1. **Build new workers image:**
   ```bash
   make docker-worker
   ```

2. **Deploy kill-switch validation:**
   ```bash
   kubectl apply -f k8s/configmaps/omni-worker-config.yaml
   ```

3. **Roll out workers:**
   ```bash
   kubectl rollout restart deployment/omni-worker -n multi-agent
   ```

4. **Verify gate:**
   ```bash
   kubectl logs -l app=omni-worker -n multi-agent | grep advisory_mode_gate_passed
   ```

5. **Test with sample alert:**
   ```bash
   # Send test evidence to Kafka
   curl -X POST http://localhost:8000/playbooks -d '{"text": "test alert", "chat_id": 123}'
   
   # Check Telegram for advisory message (should appear within 5s)
   ```

---

## Rollback Plan

If Advisory Mode causes issues:

1. **Disable advisory analyst (use old planner):**
   ```yaml
   OMNI_ADVISORY_MODE_ENABLED: "false"
   ```

2. **Re-enable auto-execute (ONLY if rolling back entirely):**
   ```yaml
   OMNI_AUTO_EXECUTE_ENABLED: "true"  # ⚠️ High risk
   ```

3. **Rollout restart workers:**
   ```bash
   kubectl rollout restart deployment/omni-worker -n multi-agent
   ```

---

## Monitoring

**Alert if:**
- `advisory_analyst_error` rate > 1 per minute
- `telegram_send_error` rate > 5 per minute
- `kill_switch_blocked` events in logs (indicates mutation attempt)
- `advisory_validation_failed` events (indicates LLM trying to emit mutations)

**Dashboard queries:**
```promql
# Advisory analyst success rate
rate(advisory_analyst_ok[5m]) / (rate(advisory_analyst_ok[5m]) + rate(advisory_analyst_error[5m]))

# Kill-switch trigger rate
rate(kill_switch_blocked[5m])

# Telegram delivery rate
rate(telegram_send_ok[5m]) / rate(telegram_send_requested[5m])
```
