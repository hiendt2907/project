# Tài liệu, nghiệm thu & báo cáo trong repo

Đây là **bản gốc** trong Git (không trùng với các trang wiki tóm tắt ở mục Architecture / Runbooks).

| Thư mục | Nội dung |
|---------|----------|
| **`project-docs/`** → `docs/` | Chỉ mục master, vendor canonical, architecture, runbooks, **báo cáo phase / chaos / E2E** trong `docs/reports/`, template nghiệm thu, v.v. |
| **`repo-reports/`** → `reports/` | Báo cáo phase cũ / flow (root), JSON artifact (build không render JSON). |

**Bắt đầu nhanh**

- [DOCUMENTATION_INDEX.md](project-docs/DOCUMENTATION_INDEX.md) — bản đồ tầng 0–4, pointer tới canonical + báo cáo.
- [docs/reports/README.md](project-docs/reports/README.md) — danh sách báo cáo trong `docs/reports/`.
- [vendor/OMNI_PROJECT_CANONICAL.md](project-docs/vendor/OMNI_PROJECT_CANONICAL.md) — kiến trúc vận hành một nguồn.

Các link tương đối trong các file gốc (ví dụ `vendor/…`, `reports/…`) giữ nguyên khi đọc trong wiki vì cùng cây thư mục `project-docs/`.
