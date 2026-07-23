# `docs/reports/` — point-in-time reports còn current

Dọn dẹp 2026-07-22: xoá phase-1..7 report/review, chaos-rag-selflearn snapshot, audit-snapshot cũ,
trace-audit/e2e-log-analysis cũ, và các plan/report đã DONE hoặc bị `CLAUDE.md`/`ASSESSMENT_autonomous_sre_v2.md`
thay thế. Xem [`../DOCUMENTATION_INDEX.md`](../DOCUMENTATION_INDEX.md) Tầng 1 cho audit/capability hiện hành.

| File | Vai trò |
|------|---------|
| [`frontend-backend-logic-verification-2026-07-14.md`](frontend-backend-logic-verification-2026-07-14.md) | Release gate verification mới nhất (backend/portal/E2E) — dẫn từ `../CODEBASE.md`. |
| [`project-memory.md`](project-memory.md) | Invariants, failure patterns, guardrails. Entries ≥2026-07-14 current; entries cũ hơn là lịch sử (xem banner đầu file). |
| [`diagnostic-policy-spec.md`](diagnostic-policy-spec.md) | Spec INV_* invariants cho diagnostic policy — vẫn khớp `pkg/reasoning/diagnostic_policy.py`. |
| [`sigma-log-bypass-spec.md`](sigma-log-bypass-spec.md) | Spec Loki sustained-5xx sigma bypass (điều kiện, env, fail-closed). |
| [`incident-evidence-three-lanes.md`](incident-evidence-three-lanes.md) | Proof-of-Fault ba lane (`resource`/`state`/`app_log`) + matrix. |
| [`dashboard-source-of-truth.md`](dashboard-source-of-truth.md) | Grafana dashboard SoT, đối chiếu `k8s/monitor/grafana-dashboards.yaml`. |
| [`e2e-artifacts/`](e2e-artifacts/) | Staging layout cho artifact E2E local (không commit secret) — dùng bởi `scripts/gateway_alert_loki_verify.sh` và tương đương. |
