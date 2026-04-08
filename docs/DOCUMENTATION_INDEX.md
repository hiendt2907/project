# Omni — chỉ mục tài liệu (chuẩn hoá)

**Đọc trước — một nguồn bám code:** [vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md)

## Tầng 1 — vận hành & kiến trúc hiện tại

| Tài liệu | Vai trò |
|----------|---------|
| [vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) | Topology split, Kafka, RAG, verify, corpus rules |
| [vendor/knownbase.md](vendor/knownbase.md) | Symptom → fix (incident thật) |
| [reports/project-memory.md](reports/project-memory.md) | Invariants, failure patterns, guardrails |
| [vendor/master_plan_v3_review_report.md](vendor/master_plan_v3_review_report.md) | MPV3 review + lịch sử + §15 nợ |
| [omni_playbook_index.md](omni_playbook_index.md) | Pointer RAG / retrieval surfaces |

## Tầng 2 — runbook & SLO

| Tài liệu | Vai trò |
|----------|---------|
| [proactive_slo.md](proactive_slo.md) | PromQL proactive |
| [proactive_state_machine.md](proactive_state_machine.md) | Phase proactive |
| [runbooks/](runbooks/) | Checklist, trace proof, E2E matrix |

## Tầng 3 — lịch sử phase / báo cáo

| Vị trí | Ghi chú |
|--------|---------|
| [reports/](reports/) | Phase 1–7, chaos-rag, templates — **artifact theo thời điểm** |
| [reports/README.md](reports/README.md) | Index phase reports |

## Tầng 4 — vendor mirror (ngoài Omni)

| Vị trí | Ghi chú |
|--------|---------|
| [vendor/README.md](vendor/README.md) | Sync vendor docs; không trùng knownbase |

## File ở root repo (`*.md`)

Các file như `architecture_analysis.md`, `*_plan.md` là **snapshot / planning** — có thể lệch runtime. Luôn đối chiếu [docs/vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) trước khi tin vào sơ đồ monolith hoặc Redis stream cũ.

## Bookmark cũ

- [vendor/golden_path_split.md](vendor/golden_path_split.md) → redirect ngắn tới canonical.
