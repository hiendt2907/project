<!-- Generated: 2026-05-22 | Token estimate: ~700 -->

# Dependencies — Omni SRE

## External Services

| Service | Endpoint | Purpose |
|---------|----------|---------|
| Ollama | `host.docker.internal:11434` (Mac host) | LLM inference (qwen3.6 35B MoE) + embeddings (nomic-embed-text 768-dim) |
| Telegram Bot API | `api.telegram.org` + chat_id=-5174042122 | Advisory notification + HITL alerts |
| Prometheus | `prometheus.monitor.svc.cluster.local:9090` | PromQL probes (Lane 1 resource + Lane 2 state) |
| Loki | `loki.monitor.svc.cluster.local:3100` | Log queries (Lane 3 APP_HTTP surge) |
| FinGuard Redis | `finguard-redis:6379` (ns: finguard-customer) | SIEM incident stream XREADGROUP |
| FinGuard HITL API | `finguard-hitl-api` (ns: finguard-customer) | HITL approval dispatch |
| MySQL / ProxySQL | via remote_agent collectors | DB health probes (radmin:radmin, read-only) |
| OpenSandbox | internal HTTP | Sandboxed command execution (SandboxManager) |

## Python Core Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | ≥0.110 | Gateway HTTP server |
| `aiokafka` | ≥0.11 | Async Kafka producer/consumer |
| `redis[hiredis]` | ≥5.0 | Redis Stack client + HNSW |
| `kubernetes-asyncio` | ≥30.0 | K8s SDK probes (read-only) |
| `pydantic` / `pydantic-settings` | ≥2.0 | Schema validation + OMNI_* env config |
| `openai` | ≥1.35 | Ollama-compatible OpenAI SDK (VLLMClient) |
| `cryptography` | ≥42.0 | Ed25519 CRAT signing |
| `prometheus-client` | ≥0.21 | Metrics exposition :9090 |
| `numpy` / `scipy` | — | 3σ z-score + linear forecast |
| `prophet` | ≥1.1 | Long-horizon forecast |
| `pandas` | — | Prometheus DataFrame conversion |
| `matplotlib` | — | Chart PNG generation (chart_bytes.py) |
| `fakeredis` | ≥2.23 | Test isolation (`FakeAsyncRedis(decode_responses=True)`) |
| `pytest-asyncio` | ≥0.23 | Async test runner (`asyncio_mode=auto`) |

## Infrastructure Dependencies

| Component | Version | Notes |
|-----------|---------|-------|
| Kafka | KRaft single-broker | 5 restarts (normal); consumer rebalance ~8s on pod restart |
| Redis Stack | StatefulSet | port-forward 16379:6379 for local access |
| Traefik | Ingress controller | Routes `gateway.ai-agent.local`, `portal.ai-agent.local`, `omni.ai-agent.local` |
| OrbStack | K8s runtime | Single Mac M-series node; 192.168.139.2 ingress IP |

## Security Dependencies

| Dependency | Purpose |
|-----------|---------|
| `gitleaks` | CI secret scanning gate (`make secret-gate`) |
| Ed25519 PEM key | CRAT block signing (`OMNI_AUDIT_PRIVATE_KEY_PATH` K8s Secret `omni-audit-keys`) |
| `OMNI_GATEWAY_API_KEY` | Gateway master key (K8s Secret `omni-gateway-secret`) |
| `OMNI_TENANT_APIKEYS` | Per-tenant API keys (`tenant_id:key` comma-separated) |

## Smart-SIEM (Go brain-go)

| Dependency | Purpose |
|-----------|---------|
| Go stdlib + kafka-go | Event correlator (`BRAIN_TRANSPORT=redis\|kafka`) |
| Redis ZSET window | CorrelatingPublisher: event correlation |
| LLM HTTP client | Kill-chain analysis via Ollama |

## Remote Agent (Python, external hosts)

```
src/remote_agent/          Python async collector (v1.1.3) — CANONICAL, deployed to Linux hosts/VMs
Collectors: system · k8s · database (MySQL/ProxySQL) · logs · storage
Auth: OMNI_AGENT_SECRET (pre-shared key in /agent/push route)
```

## CI/CD

```
make secret-gate        gitleaks scan
make autonomy-gate      autonomy regression tests (pass=true ✓)
make coverage-gate      90% coverage threshold
.github/workflows/ci.yml  build → rollout → unit → E2E
```
