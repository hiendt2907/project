# Omni internal wiki (MkDocs Material)

- **Config:** `mkdocs.yml`
- **Pages:** `docs/`
- **Build output:** `site/` (gitignored; produced by `mkdocs build`)

## Knowledge base (tri thức hệ thống)

Thư mục `wiki/docs/knowledge-base/`: hợp đồng Kafka/topology, invariants từ `project-memory`, guardrails cho AI/Claude, reason codes, ma trận verify/gate — **tổng hợp có trích dẫn file nguồn**, không thay thế đọc trực tiếp `docs/vendor/OMNI_PROJECT_CANONICAL.md` khi làm thay đổi hành vi.

## Thư viện repo

Trong nav MkDocs, mục **Tài liệu & báo cáo (repo)** nhúng symlink:

- `wiki/docs/library/project-docs` → `docs/` (tài liệu, báo cáo phase, runbooks, vendor…)
- `wiki/docs/library/repo-reports` → `reports/` (báo cáo root)

Một số link trong file gốc trỏ tới `../src/`, `../k8s/` — MkDocs không build những path đó; khi cần mở trong IDE/Git.

Build wiki **không** dùng `--strict` mặc định (tránh fail vì link tương đối ra ngoài `docs/`). Bật kiểm tra chặt: `WIKI_STRICT=1 bash scripts/wiki_build.sh`.

## Quick start

**Khuyến nghị** (tắt banner đỏ MkDocs 2.0 + log sạch như CI):

```bash
bash scripts/wiki_serve.sh
# → http://127.0.0.1:9001 (dev_addr trong mkdocs.yml)
```

Hoặc tay:

```bash
.venv/bin/pip install -r wiki/requirements-docs.txt
NO_MKDOCS_2_WARNING=1 .venv/bin/mkdocs serve -f wiki/mkdocs.yml
```

(`NO_MKDOCS_2_WARNING` được set mặc định trong `scripts/wiki_serve.sh` và `scripts/wiki_build.sh`; `wiki/mkdocs.yml` có `validation` để không spam WARNING link từ thư mục `docs/` nhúng symlink.)

**Do not** paste `then` after `pip install` (that was interpreted as a bogus package name).

**Port already in use:** pick another port, e.g.

```bash
.venv/bin/mkdocs serve -f wiki/mkdocs.yml -a 127.0.0.1:8010
```

Or:

```bash
bash scripts/wiki_build.sh   # uses mkdocs on PATH; set WIKI_INSTALL_DEPS=1 to pip install first
```

## CI

GitHub Actions workflow `.github/workflows/wiki.yml` runs on changes to `src/`, `docs/`, `reports/`, `wiki/`, or `scripts/wiki_build.sh`, uploads `omni-wiki-site` artifact.

## Hosting

Point static hosting (GitHub Pages, S3, internal nginx) at the contents of `wiki/site/` after build. Override `site_url` in `mkdocs.yml` for your internal URL.
