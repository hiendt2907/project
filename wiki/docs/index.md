# Omni Lab — Internal Wiki

**Wiki + thư viện repo** phục vụ vận hành, nghiệm thu, và **tri thức cho agent** (Cursor / Claude / pipeline Omni). Nguồn chân lý vẫn là code + `docs/vendor/OMNI_PROJECT_CANONICAL.md`.

| Section | Contents |
|---------|----------|
| [Architecture](architecture/overview.md) | Split topology (MPV3), Kafka flow |
| [Three lanes](architecture/three-lanes.md) | Resource / State / App log proof model |
| [Module map](modules/module-map.md) | What key Python modules do |
| [Gateway API](api/gateway.md) | FastAPI ingress contract |
| [Runbooks](runbooks/evidence-and-sigma.md) | Troubleshooting by lane and symptom |
| [**Knowledge base**](knowledge-base/index.md) | **Hợp đồng hệ thống, invariants, guardrails AI, reason codes, gates** |
| [**Tài liệu & báo cáo gốc (repo)**](library/index.md) | Toàn bộ `docs/` + `reports/` (chỉ mục, phase, nghiệm thu, vendor) |

**Build / preview locally** (repo root, venv recommended)

```bash
bash scripts/wiki_serve.sh
```

(Hoặc `NO_MKDOCS_2_WARNING=1 .venv/bin/mkdocs serve -f wiki/mkdocs.yml` — tránh banner MkDocs 2.0.)

Preview defaults to `http://127.0.0.1:9001`. If `Address already in use`, run with e.g. `-a 127.0.0.1:9002`.

Static build: `bash scripts/wiki_build.sh` → output `wiki/site/`. Strict (hay fail vì link ra `src/`): `WIKI_STRICT=1 bash scripts/wiki_build.sh`.

**CI:** `.github/workflows/wiki.yml` — cài deps, build wiki khi đổi `src/`, `docs/`, `reports/`, hoặc `wiki/`.
