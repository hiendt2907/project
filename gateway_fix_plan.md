# Implementation Plan: Fix Gateway Redis Cluster Support

The `omni-gateway` is currently failing to enqueue events because its connection logic only supports standalone Redis and is defaulting to a non-existent `redis:6379` host. This plan will update the gateway to use the correct Redis Cluster configuration.

## User Review Required

> [!IMPORTANT]
> This requires modifying the `omni-gateway-source` ConfigMap and restarting the gateway. The gateway will be temporarily unavailable during the rolling update.

## Proposed Changes

### 1. Gateway Logic Update

#### [MODIFY] [omni-gateway-source](kubectl-configmap)
*   **Update** `api.py`:
    *   Import `RedisCluster` from `redis.asyncio.cluster`.
    *   Read `OMNI_REDIS_CLUSTER` and `OMNI_REDIS_CLUSTER_NODES` environment variables.
    *   Implement conditional initialization: Use `RedisCluster.from_url` if clustering is enabled, otherwise fallback to `aioredis.from_url`.

### 2. Deployment Refresh

#### [RESTART] [omni-gateway](kubectl-deployment)
*   Trigger a rollout restart to pick up the ConfigMap changes: `kubectl rollout restart deployment omni-gateway -n multi-agent`.

## Verification Plan (The "Proof")

### Automated Tests
*   **Proof A**: `kubectl logs -n multi-agent deployment/omni-gateway`: Verify "omni-gateway started" with cluster info.
*   **Proof B**: Re-run the `curl` test event and capture a `200 OK` response with a `trace_id`.
*   **Proof C**: `kubectl exec ... redis-cli -c XLEN events:inbound`: Verify the stream length increments.
*   **Proof D**: `kubectl logs -n multi-agent deployment/omni-worker`: Capture the log line showing the worker processing the specific `trace_id`.

### Manual Verification
*   Confirm the message appears in the Telegram admin chat (if enabled).
