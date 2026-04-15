# Runbook: Gateway & ingest

## Symptom: `429 Too Many Requests`

**Cause:** Token bucket rate limit (`OMNI_GATEWAY_RATE_LIMIT_TPS`).

**Actions:** Reduce alert fan-in at Alertmanager; shard receivers; or raise TPS **only** if Kafka/prober capacity allows (coordinate with infra).

## Symptom: `503` + circuit breaker

**Cause:** Redis key `omni:circuit_breaker:active` == `1` (worker-side backpressure).

**Actions:**

1. Inspect prober/worker health and Kafka lag.
2. Clear breaker only after root cause fixed (process that sets the key must be understood—search repo for `omni:circuit_breaker:active`).

## Symptom: Alerts not reaching Kafka

**Checks**

1. Gateway logs for `kafka_enqueued` with `trace_id`.
2. `POST /webhook/prometheus` returns 200 `queued` (not `dropped`).
3. Chaos lab silencing: `OMNI_GATEWAY_SILENCE_CHAOS_LAB` drops validation webhooks.

**Actions:** Validate Kafka bootstrap DNS from gateway pod, topic exists (`make ensure-kafka-topics`), ACLs.

## Symptom: Trace ID mismatch downstream

**Rule:** Honor client `X-Omni-Trace-Id` or `?trace_id=` when valid; else gateway generates new id.

**Actions:** Configure Alertmanager or proxies to forward trace header end-to-end for correlation.

## Health checks

- `GET /healthz` — liveness-style JSON.
- `GET /metrics` — scrape for `omni_gateway_requests_total` by status label.
