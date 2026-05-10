# Gateway API contract (`src/gateway/api.py`)

FastAPI application **Omni Gateway** v1.0.0. All webhook responses include `X-Omni-Trace-Id` header when a body is returned.

## Environment (operational)

| Variable | Purpose |
|----------|---------|
| `OMNI_REDIS_URL` | Redis for circuit breaker + rate-limit state (default `redis://redis:6379/0`) |
| `OMNI_KAFKA_BOOTSTRAP_SERVERS` | Kafka bootstrap (default `kafka:9092`) |
| `OMNI_KAFKA_TOPIC_ALERTS` / `OMNI_STREAM_INBOUND` | Topic for alerts (default `omni-alerts`) |
| `OMNI_GATEWAY_RATE_LIMIT_TPS` | Token-bucket rate limit (default `1000`) |
| `OMNI_GATEWAY_SILENCE_CHAOS_LAB` | If true, drops chaos-lab shaped webhooks with 200 |

## Endpoints

### `GET /healthz`

**200** JSON:

```json
{"status": "ok", "rate_limit_tps": <int>}
```

### `GET /metrics`

Prometheus text exposition (Prometheus client `generate_latest`).

### `GET /metrics/circuit_breaker`

**200** JSON:

```json
{"circuit_breaker_active": bool}
```

Reads Redis key `omni:circuit_breaker:active` (`"1"` == active).

### `POST /webhook/prometheus`

Alertmanager/Prometheus webhook payload (JSON body).

**Trace ID resolution**

1. Valid `X-Omni-Trace-Id` header or `?trace_id=` query: `[a-zA-Z0-9_-]{8,128}` — **honored**.
2. Else generated: `gw-prom-<12 hex chars>`.

**Processing order**

1. **Rate limit:** semaphore refill per second; if no token → **429** with `error`, `detail`, `trace_id`.
2. **Circuit breaker:** if Redis flag active → **503** with `error`, `detail`, `trace_id`.
3. **Parse JSON** body (empty dict on failure).
4. Optional chaos-lab drop when `OMNI_GATEWAY_SILENCE_CHAOS_LAB` enabled → **200** `status: dropped`.
5. **Kafka produce** to `OMNI_KAFKA_TOPIC_ALERTS` with envelope:

```json
{"data": "<stringified JSON of payload below>"}
```

Inner payload:

```json
{
  "source": "prometheus",
  "trace_id": "<id>",
  "received_at": <unix float>,
  "data": <original Alertmanager body>
}
```

**200** success:

```json
{"status": "queued", "trace_id": "<id>"}
```

**500** on unexpected errors (`HTTPException` with detail including `trace_id`).

## Kafka payload shape

Consumers must parse the outer `data` field and then the inner JSON to recover `trace_id` and `data` (alert list).

## Security note

Gateway must **not** import `src/workers/**`, `pkg/reasoning`, or `pkg/executor` (enforced by project rules).
