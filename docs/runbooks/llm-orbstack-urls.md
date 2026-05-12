# LLM endpoints — OrbStack and in-cluster

Omni uses OpenAI-compatible URLs (`…/v1`) pointing at Ollama.

## Environment

| Variable | Purpose |
|----------|---------|
| `OMNI_VLLM_BASE_URL` | Chat/completions base (preferred). |
| `OMNI_OLLAMA_BASE_URL` | Alias for `OMNI_VLLM_BASE_URL` (legacy single-host setups). |
| `OMNI_VLLM_EMBED_URL` | Embeddings base. |
| `OMNI_OLLAMA_EMBED_URL` | Alias for embed URL. |
| `OMNI_LLM_TIMEOUT_SEC` | HTTP client timeout + asyncio gates (`llm_chat_timeout_sec`). |
| `OMNI_LLM_NUM_PARALLEL` | Semaphore slots (`llm_num_parallel`). |

Trailing `/v1` is optional; URLs are normalized once.

## Where to point Ollama

| Runtime | Typical base URL |
|---------|-------------------|
| Python on Mac (OrbStack VM) | `http://host.orb.internal:11434` |
| Omni pod on OrbStack K8s | `http://host.orb.internal:11434` (same DNS from pods) |
| In-cluster Ollama service | `http://ollama-service.multi-agent.svc.cluster.local:11434` |

Do **not** override Ollama `num_ctx` from defaults (Omni invariant `4096` context routing stays in worker settings).
