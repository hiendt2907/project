# Vendor Docs Local Mirror

**Omni project canonical (architecture + ops):** `[OMNI_PROJECT_CANONICAL.md](./OMNI_PROJECT_CANONICAL.md)` — single source grounded in code; start here before vendor mirrors.

This directory stores a local mirror of official documentation pages used by this project.

Canonical path: `docs/vendor/` (always use this spelling in rules, links, and CI).

**Doc map (Omni-owned):** [../DOCUMENTATION_INDEX.md](../DOCUMENTATION_INDEX.md) — vendor HTML mirrors live **below** this README; project truth stays in `OMNI_PROJECT_CANONICAL.md` and `knownbase.md`.

## Project known issues (not vendor mirrors)

Operational fixes (symptom → fix) belong in `[knownbase.md](./knownbase.md)` (same directory). Cursor agents are instructed in `.cursorrules` (**KNOWN ISSUES / KNOWLEDGEBASE**) to update that file after resolving real bugs or infra issues. **Do not** add `knownbase.md` to `sources.json` or sync it via `sync_vendor_docs.py`—it is project-maintained only.

## Why

- Reduce repeated internet searches during coding/refactor sessions.
- Keep a searchable local snapshot for core dependencies and platform tools.
- Make agent context more deterministic and faster.

## Source manifest

- `docs/vendor/sources.json`

## Sync command

```bash
python scripts/sync_vendor_docs.py
```

Optional:

```bash
python scripts/sync_vendor_docs.py --timeout 40
python scripts/sync_vendor_docs.py --manifest docs/vendor/sources.json
```

## Current vendor set (mirrored from `sources.json`)

Aligned with `**requirements.txt**`, `**k8s/**` images, and core integrations:

**Language / runtime:** Python  
**Data & API:** PostgreSQL, asyncpg, pgvector, Redis, Qdrant  
**Orchestration:** Kubernetes (+ kube-state-metrics doc page), Kubernetes Asyncio SDK  
**HTTP / gateway:** FastAPI, Uvicorn, HTTPX, aiohttp  
**Config / models:** Pydantic, pydantic-settings, PyYAML  
**LLM:** Ollama, Google Gemini API (`google-genai`)  
**Observability:** Prometheus, VictoriaMetrics, Grafana, Grafana Loki, Grafana Tempo, Grafana Mimir, Promtail (via Loki docs), OpenTelemetry OTLP, prometheus-client (Python), redis_exporter, node_exporter  
**Messaging / UX:** Telegram Bot API  
**Numerics / viz / forecast:** NumPy, pandas, SciPy, Matplotlib, Prophet  
**Systems / net:** psutil, Scapy  
**Resilience / logging / test:** Tenacity, python-json-logger, pytest, pytest-asyncio, fakeredis, vulture  
**Registry / sidecar:** Docker Distribution (registry:2), Kiwigrid k8s-sidecar  

**Not in this repo’s stack (example only):** If sếp later puts **HAProxy** (or another L7 LB) in front of the gateway, add official pages under a new vendor block in `sources.json` (same pattern as other URLs)—the project today uses in-cluster **Ingress / Services** and the **Omni Gateway** (FastAPI), not HAProxy.

## Notes

- The sync stores raw page content as `.html` for robust capture with zero extra dependencies.
- Update `sources.json` to add/remove official pages as needed.
- For large “full docs” mirrors, prefer adding only high-value sections first to control repo size.
- After editing the manifest, run `python scripts/sync_vendor_docs.py` to fetch HTML into each `base_dir` (new dirs appear on first sync).
- Some sources (e.g. GitHub `README` / PyPI) return HTML that is noisier than vendor doc sites; trim `pages` if sync is slow or flaky.

