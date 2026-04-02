# Chặng 4 — Trái tim hệ thống (Omni Worker)

## Đã giao

| Thành phần | Đường dẫn | Mô tả |
|------------|-----------|--------|
| Settings | [src/workers/settings.py](src/workers/settings.py) | `OMNI_*` prefix: Redis URL, stream `events:inbound` / DLQ `events:dlq`, consumer group, `ollama_num_parallel`, models. |
| Semaphore | [src/workers/ollama_semaphore.py](src/workers/ollama_semaphore.py) | Pool Redis `LPOP`/`RPUSH` + lease TTL + `reconcile_pool_leaks`. |
| Tools | [src/workers/tools.py](src/workers/tools.py) | Registry + `echo` mẫu. |
| Handlers | [src/workers/handlers.py](src/workers/handlers.py) | Fast-Path: embed + `query_points` Qdrant ≥ score + `auto_execute` → tool. Slow-Path: **sau** `acquire` semaphore, tối đa 3 vòng chat JSON + tool, feedback lỗi tiếng Việt. |
| Omni | [src/workers/omni_worker.py](src/workers/omni_worker.py) | `asyncio.gather(stream_loop, telegram_loop?)`; DLQ `XADD` + luôn `XACK`; `ErrorLedger` dùng chung client Qdrant. |
| Docker | [Dockerfile](Dockerfile) | `USER appuser` (uid 10001), `CMD python -m workers`. |
| K8s | [k8s/deployments/omni-worker.yaml](k8s/deployments/omni-worker.yaml) | Deployment `multi-agent`; Telegram tắt mặc định (`OMNI_TELEGRAM_ENABLED=false`). |

## Chạy local / cluster

```bash
# Test
pytest tests/ -q

# Image (tag cố định)
docker build -t multi-agent-system:latest -f Dockerfile .

# Apply (sau Redis/Qdrant; cần Service ollama-service hoặc sửa env)
kubectl apply -f k8s/deployments/omni-worker.yaml
kubectl rollout status deployment/omni-worker -n multi-agent --timeout=120s
```

## Env quan trọng

- `OMNI_REDIS_URL` — `redis://redis:6379/0`
- `QDRANT_URL` — `http://qdrant:6333`
- `OMNI_OLLAMA_BASE_URL` — `http://ollama-service:11434`
- `OMNI_CHAT_MODEL` — trùng `ollama list` (mặc định: `qwen2.5:7b`)
- `OMNI_EMBED_MODEL` — embed Fast-Path (mặc định: `nomic-embed-text:latest`)
- `TELEGRAM_BOT_TOKEN` — bật `OMNI_TELEGRAM_ENABLED=true` nếu cần polling

### Model có trên máy (ví dụ)

| Tag | Gợi ý |
|-----|--------|
| `qwen2.5:7b` | Mặc định manifest — cân bằng tốc/chất lượng |
| `qwen2.5:1.5b` | Nhẹ, phản hồi nhanh |
| `deepseek-r1:8b` | Reasoning |
| `gemma3:4b` / `gemma3:27b` | Nặng hơn (27b ~17GB) |
| `nomic-embed-text:latest` | Embed Qdrant |

## Ollama trên Mac (OrbStack)

Áp [k8s/deployments/ollama-service-external.yaml](../k8s/deployments/ollama-service-external.yaml): `ExternalName` → `host.docker.internal:11434` để Pod gọi Ollama chạy trên máy host.

## Kiểm tra thực tế (hiendang / OrbStack)

- Namespace `monitor` + `multi-agent`: VM, Redis, Qdrant, omni-worker **Running**.
- Trước khi có ExternalName: `ollama-service` không resolve; sau khi áp manifest → `GET http://ollama-service:11434/api/tags` **200** từ trong Pod.
- Đã đổi default: **`qwen2.5:7b`** + **`nomic-embed-text:latest`** (khớp `ollama list`).
- Qdrant: cảnh báo client 1.17 vs server 1.12 — nên nâng image Qdrant hoặc hạ client cho khớp minor.
- Model rất lớn (**gemma3:27b**): inference có thể **lâu**; `XPENDING` có thể >0 trong lúc chạy.

## Nghiệm thu

- [x] Fast-Path trước semaphore; Slow-Path sau `acquire`.
- [x] Lỗi handler → DLQ stream + `XACK`; không kẹt PEL.
- [x] `pytest` + `docker build` đã chạy thành công trên môi trường build.

## E2E Telegram (input → output)

Xem [e2e-telegram-flow.md](e2e-telegram-flow.md) và script [scripts/follow-trace.sh](../scripts/follow-trace.sh) — `trace_id` dạng `tg-{chat_id}-{update_id}-{message_id}` trong log.
