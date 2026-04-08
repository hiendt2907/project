# Proactive SLO (lab)

Short reference for Prometheus queries tied to the proactive daemon. Histogram and counters are emitted from the worker codebase on the deployment that runs the proactive observer — in **split topology**, use **`omni-core`** (`src/workers/metrics_exporter.py`, `src/workers/proactive_observer.py`). Legacy monolith: `omni-worker` if `OMNI_WORKER_ROLE` includes proactive.

See also: [OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) §6 (metrics by deployment).

## Incident duration (end-to-end handler)

**Metric:** `omni_proactive_incident_duration_seconds` (Histogram)

**Definition:** Wall time from successful proactive semaphore acquire to handler return in `_process_proactive_message` (includes SOP, learning, governance, ReAct, and `EVENT_TIMEOUT`). **Not** recorded for `SKIPPED_KILL_SWITCH` or malformed payload (`FAIL` parse) because those paths never acquire the semaphore.

**p95 (5m):**

```promql
histogram_quantile(0.95, sum(rate(omni_proactive_incident_duration_seconds_bucket[5m])) by (le))
```

**p99 (10m):**

```promql
histogram_quantile(0.99, sum(rate(omni_proactive_incident_duration_seconds_bucket[10m])) by (le))
```

**Suggested lab threshold:** p99 > 300s for 15m → investigate slow LLM, tool timeouts, or `proactive_event_timeout_sec` (see `k8s/monitor/prometheus.yaml` rule `OmniProactiveIncidentSlow`).

## Outcome mix (ReAct)

**Metric:** `omni_proactive_outcome_total` with label `outcome` (`sop_success`, `react_resolved`, `react_escalated`, `governance_deny`, …).

**Share of ReAct runs that escalated (1h):**

```promql
100 * (
  sum(increase(omni_proactive_outcome_total{outcome="react_escalated"}[1h]))
  / clamp_min(sum(increase(omni_proactive_outcome_total{outcome=~"react_escalated|react_resolved"}[1h])), 1)
)
```

Tune window and thresholds per environment; do not add high-cardinality labels (for example `trace_id`) to these series.

## Verification health

**Metric:** `omni_proactive_verify_total{outcome=...}`

**Success ratio (5m)** — same as L0 dashboard:

```promql
100 * (
  sum(increase(omni_proactive_verify_total{outcome="success"}[5m]))
  / clamp_min(sum(increase(omni_proactive_verify_total[5m])), 1)
)
```

## Related doc

- State machine and exit paths: [`docs/proactive_state_machine.md`](proactive_state_machine.md)
