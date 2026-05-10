# `docs/reports/` — báo cáo phase & artifact

**Kiến trúc sống:** [../vendor/OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md) — các file dưới đây là **lịch sử / planning / artifact theo thời điểm**.

## Cross-cutting

| File | Ghi chú |
|------|---------|
| [project-memory.md](project-memory.md) | Invariants, failure patterns. |
| [unified-document-report.md](unified-document-report.md) | Báo cáo tổng hợp doc. |
| [dashboard-source-of-truth.md](dashboard-source-of-truth.md) | Dashboard SoT. |

## Phase 1–7 (report + review)

| Phase | Report | Review |
|-------|--------|--------|
| 1 | [phase-1-report.md](phase-1-report.md), [phase-1-state-machine-report.md](phase-1-state-machine-report.md) | [phase-1-review.md](phase-1-review.md) |
| 2 | [phase-2-report.md](phase-2-report.md), [phase-2-test-pyramid-report.md](phase-2-test-pyramid-report.md) | [phase-2-review.md](phase-2-review.md) |
| 3 | [phase-3-report.md](phase-3-report.md), [phase-3-e2e-verification-report.md](phase-3-e2e-verification-report.md) | [phase-3-review.md](phase-3-review.md) |
| 4 | [phase-4-report.md](phase-4-report.md), [phase-4-adapterization-report.md](phase-4-adapterization-report.md) | [phase-4-review.md](phase-4-review.md) |
| 5 | [phase-5-report.md](phase-5-report.md), [phase-5-slo-gates-report.md](phase-5-slo-gates-report.md) | [phase-5-review.md](phase-5-review.md) |
| 6 | [phase-6-report.md](phase-6-report.md) | [phase-6-review.md](phase-6-review.md) |
| 7 | [phase-7-report.md](phase-7-report.md) | [phase-7-review.md](phase-7-review.md) |

## Chaos / RAG self-learn

| File |
|------|
| [chaos-rag-selflearn-runbook.md](chaos-rag-selflearn-runbook.md) |
| [chaos-rag-selflearn-export-ingest.md](chaos-rag-selflearn-export-ingest.md) |
| [chaos-rag-selflearn-schema-jsonl.md](chaos-rag-selflearn-schema-jsonl.md) |
| [chaos-rag-selflearn-learning-delta.md](chaos-rag-selflearn-learning-delta.md) |
| [chaos-rag-selflearn-uat-checklist.md](chaos-rag-selflearn-uat-checklist.md) |
| [templates/chaos-rag-phase-report.template.md](templates/chaos-rag-phase-report.template.md) |
| [templates/chaos-rag-session-report.template.md](templates/chaos-rag-session-report.template.md) |

## Templates (repo root `docs/`)

- [../phase_report_template.md](../phase_report_template.md)
- [../phase_review_template.md](../phase_review_template.md)

## Status board (tóm tắt)

- Phase 1–7: xem bảng trên; một số phase vẫn `planned` trong backlog autonomy — đối chiếu code + canonical.
