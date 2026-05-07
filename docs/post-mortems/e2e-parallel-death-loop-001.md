# Post-Mortem: E2E Parallel Death Loop — Lane 3 & Lane 4 Failures

**Date:** 2026-05-06
**Severity:** Medium (test infrastructure, not production)
**Status:** Resolved

---

## Summary

During the build-out of `scripts/e2e_parallel_death_loop.py` — a harness that exercises all 4 diagnostic lanes concurrently via `asyncio.gather()` — two distinct failures were discovered and fixed:

1. **Lane 3 (APP_HTTP):** `UnknownTopicOrPartitionError` from the aiokafka producer, caused by blocking `time.sleep()` starving the asyncio event loop while a Kafka `send()` future was pending.
2. **Lane 4 (SIEM_SECURITY):** Log search returned no results because the siem-bridge silently rewrites the injected `trace_id` to a canonical value, making the injected trace useless as a search key.

Both bugs were latent in the previous single-lane sequential scripts; they only surfaced when all 4 lanes ran in parallel with shared asyncio resources.

---

## Timeline

| Time (relative) | Event |
|---|---|
| T+0 | `scripts/e2e_parallel_death_loop.py` first run: Lanes 1 and 2 pass; Lane 3 and Lane 4 both fail |
| T+5m | Lane 3 error identified: `aiokafka.errors.UnknownTopicOrPartitionError` on `omni-diagnostic-evidence` topic |
| T+20m | Root cause for Lane 3 confirmed: `time.sleep()` in async context blocks event loop; `producer.send()` future never resolves; TCP connection times out after 240s; stale broker metadata causes topic error |
| T+25m | Lane 3 fix applied: `_wait_logs()` converted to async using `asyncio.sleep()` + `asyncio.to_thread()` for subprocess; `_publish_evidence()` changed to `send_and_wait()` |
| T+30m | Lane 3 now passes with count_429=42, sigma_bypass=True, dominant_error_class=rate_limit |
| T+35m | Lane 4 failure investigated: E2E injected trace `fg-<random-suffix>` into stream:actionable_incidents; Loki query by that trace returns 0 lines |
| T+55m | Root cause for Lane 4 confirmed: siem-bridge (`src/services/evidence_adapter/siem_bridge.py`) rewrites `trace_id` on all forwarded incidents to the canonical value `fg-e2e-siem`, discarding the original |
| T+60m | Lane 4 fix applied: Loki search key changed from injected trace → `incident_id` (preserved in log labels); feedback published using canonical trace `fg-e2e-siem` read from Loki output |
| T+65m | All 4 lanes pass in parallel run; CRAT signing confirmed (`signed=True`, ~18ms per audit block) |

---

## Root Cause Analysis

### Lane 3: asyncio event loop blocking (UnknownTopicOrPartitionError)

**What happened:**

`_wait_logs()` used the standard `time.sleep()` call inside an async function to poll Loki:

```python
async def _wait_logs(pattern, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(["logcli", "query", ...], capture_output=True)
        if pattern in result.stdout:
            return result.stdout
        time.sleep(5)   # <-- BLOCKS the entire event loop thread
```

Simultaneously, `_publish_evidence()` issued a `producer.send(topic, value)` call which returns a `Future` that must be resolved by the event loop:

```python
future = await producer.send("omni-diagnostic-evidence", value=payload)
# event loop is needed to resolve this future
```

Because `time.sleep(5)` released no control to the event loop, the Kafka `send()` future remained pending. After ~240 seconds of no activity, the broker dropped the TCP connection. On the next send attempt, the aiokafka client held stale partition metadata and raised `UnknownTopicOrPartitionError` for `omni-diagnostic-evidence` — a topic that clearly exists.

**Contributing factor:** This pattern had been safe in previous sequential single-lane scripts because each lane's `_wait_logs()` call completed before the next Kafka send. In the parallel harness, multiple lanes' polling and produce calls overlap, making the event loop starvation immediately fatal.

**Fix:**

```python
async def _wait_logs(pattern, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await asyncio.to_thread(
            subprocess.run, ["logcli", "query", ...], capture_output=True
        )
        if pattern in result.stdout:
            return result.stdout
        await asyncio.sleep(5)   # yields control; event loop stays alive

async def _publish_evidence(producer, topic, payload):
    await producer.send_and_wait(topic, value=payload)  # immediate broker ack
```

`asyncio.to_thread()` runs the blocking subprocess in a thread pool without blocking the event loop. `send_and_wait()` ensures the produce is acknowledged before the coroutine continues, eliminating the pending-future problem.

---

### Lane 4: SIEM bridge trace_id override

**What happened:**

The E2E script injected a per-run trace ID (`fg-<random-suffix>`) into the Redis stream `stream:actionable_incidents`:

```python
incident = {
    "trace_id": f"fg-{run_suffix}",
    "incident_id": "siem-e2e-ddos-001",
    ...
}
redis.xadd("stream:actionable_incidents", incident)
```

The subsequent Loki search looked for this injected trace in omni-analyst logs:

```python
query = f'{{app="omni-analyst"}} |= "fg-{run_suffix}"'
```

This returned 0 lines. The incident had been processed correctly, but none of the logs referenced the injected trace.

**Root cause:** `src/services/evidence_adapter/siem_bridge.py` explicitly overwrites `trace_id` for all forwarded incidents with a canonical value:

```python
envelope["trace_id"] = "fg-e2e-siem"   # canonical — overwrites the injected value
```

This design exists so the analyst can correlate all SIEM traffic under a stable trace key. The `incident_id` field is preserved in Kafka message labels and log output, but the original `trace_id` is silently discarded.

**Fix:**

1. Search Loki by `incident_id` instead of `trace_id`:
   ```python
   query = f'{{app="omni-analyst"}} |= "siem-e2e-ddos-001"'
   ```
2. Extract the canonical trace from the Loki output (`fg-e2e-siem`) and use it when publishing the feedback message to `omni-action-feedback`, instead of using the injected trace.

This is correct behaviour by the bridge; the E2E harness was wrong to assume the trace would be preserved.

---

## Fix Summary

| Lane | File changed | Change |
|---|---|---|
| Lane 3 | `scripts/e2e_parallel_death_loop.py` | `_wait_logs()`: `time.sleep` → `asyncio.sleep` + `asyncio.to_thread` for subprocess |
| Lane 3 | `scripts/e2e_parallel_death_loop.py` | `_publish_evidence()`: `producer.send()` → `producer.send_and_wait()` |
| Lane 4 | `scripts/e2e_parallel_death_loop.py` | Loki search key: injected `trace_id` → `incident_id` |
| Lane 4 | `scripts/e2e_parallel_death_loop.py` | Feedback trace: injected value → canonical `fg-e2e-siem` read from logs |

---

## Verification

After fixes, a full parallel run confirmed:

- **Lane 1 (SYS_RESOURCE):** 3 probes published, LLM advisory 14-26s, SUGGEST_REMEDIATION, COMMAND_FEEDBACK_INGESTED, RE_EVALUATED
- **Lane 2 (SYS_HARD_FAIL):** INVESTIGATE verdict (CrashLoopBackOff/missing ConfigMap), CRAT ADVISORY_DECISION + ADVISORY_DISPATCHED written signed=True in ~18ms, ESCALATE_TO_HUMAN after replan_empty
- **Lane 3 (APP_HTTP):** count_429=42, sigma_bypass=True, dominant_error_class=rate_limit, COMMAND_FEEDBACK_INGESTED
- **Lane 4 (SIEM_SECURITY):** SIEM fast-path + LLM advisory both executed, kill-chain forecast 5 horizons, COMMAND_FEEDBACK_INGESTED using canonical trace

Full pipeline Loki evidence for Lane 2: 42 lines (gateway:4, prober:10, analyst:25, executor:3).

---

## Lessons Learned

1. **`time.sleep()` in an async function is always wrong.** It blocks the entire event loop thread and starves all other coroutines. Asyncio lint rules should flag this at the project level.

2. **Parallel tests expose event loop hazards that sequential tests mask.** Both bugs existed before the parallel harness; they were hidden by the sequential execution order.

3. **Infrastructure components that rewrite fields must be documented as invariants.** The siem-bridge canonical trace behaviour was not documented. Any consumer that joins a pipeline mid-stream (including E2E scripts) will be misled if they assume the originator's trace is preserved.

4. **`send_and_wait()` is safer than `send()` in E2E harnesses.** For test code that needs to know a message was delivered before proceeding, `send_and_wait()` gives an immediate error instead of a delayed 240s timeout.

5. **`agg_timeout_sec=3.0s` is an E2E timing reality.** Single-probe evidence batches do not flush immediately; they flush after 3 seconds. E2E wait loops must account for this before checking Loki for analyst activity.

---

## Action Items

- [ ] Add asyncio lint rule (e.g. pylint-async or custom flake8 plugin) to flag `time.sleep()` inside `async def` functions
- [ ] Document siem-bridge canonical trace behaviour (`fg-e2e-siem`) as an INVARIANT in CLAUDE.md
- [ ] Consider making siem-bridge preserve the original `trace_id` as a secondary label (e.g. `source_trace_id`) so consumers have both the canonical and originating traces
- [ ] Add `send_and_wait()` guidance to the Kafka producer section of the developer guide
- [ ] Extend `scripts/e2e_parallel_death_loop.py` with a `--lane` flag to run individual lanes in isolation for faster debugging
