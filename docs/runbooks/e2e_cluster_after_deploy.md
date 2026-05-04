# E2E cluster checklist (sau deploy worker / gateway)

Dùng sau `make deploy-worker` hoặc rollout tương đương — xác nhận không escalate oan khi deployment healthy.

**DoD đầy đủ (trust but verify + death loop):** [e2e_full_flow_evidence_checklist.md](e2e_full_flow_evidence_checklist.md).

## Chuẩn bị

1. Context kube đúng (OrbStack): `./scripts/with_working_kube.sh kubectl get ns`
2. Image đã build theo [`.cursor/rules/omni-cicd-k8s.mdc`](../../.cursor/rules/omni-cicd-k8s.mdc).

## Kịch bản E2E (A / B / C)

| ID | Mục đích | Lệnh chính |
|----|----------|------------|
| **A** | Smoke contrast / suggest (HighCPU mặc định) | `bash scripts/gateway_alert_loki_verify.sh` |
| **B** | Full advisory LLM + CRAT + Telegram assert mặc định | `bash scripts/e2e_one_alert_full_advisory_path.sh` — mặc định `SLEEP_SEC=120`, `E2E_EXTRA_AGENTIC_SLEEP=300`, `STRICT_ASSERT_MIN_DEPLOY_HITS=2`, `STRICT_ASSERT_INCLUDE_ADVISORY_MARKERS=1`, `E2E_ASSERT_FULL_ADVISORY_LLM=1` |
| **C** | Death loop / `omni-action-feedback` → analyst tới terminal | Sau A/B có `trace_id`: `bash scripts/e2e_collect_trace_evidence.sh '<trace_id>'` + fault lab / inject (xem checklist); **chỉ** giảm `OMNI_STATE_VERIFY_MAX_ATTEMPTS` trên **lab** nếu cần chạm cap nhanh |

## Bước verify

1. **Pods:** `kubectl get pods -n multi-agent -l 'app in (omni-analyst,omni-prober,omni-core,omni-executor)'`
2. **Gateway (nếu đụng ingest):** `kubectl get pods -n multi-agent -l app=omni-gateway`
3. **E2E A hoặc B:** như bảng trên; tee log để lưu `trace_id`.
3b. **Telegram Bot API (tùy lab):** `E2E_ASSERT_TELEGRAM_BOT_API=1` + `TELEGRAM_BOT_TOKEN` — assert tin advisory qua `getUpdates` (xem [e2e_telegram_bot_api_assert.md](e2e_telegram_bot_api_assert.md); nên `OMNI_TELEGRAM_POLLING_ENABLED=false` trên prober).
4. **Loki theo trace_id:** Grafana Explore Loki hoặc log query `{namespace="multi-agent"} |= "<trace_id>"`.
5. **E2E C (death loop):** `bash scripts/e2e_collect_trace_evidence.sh '<trace_id>'` — đếm feedback / terminal; lưu output vào artifact (xem [e2e-artifacts/README.md](../reports/e2e-artifacts/README.md)).
6. **Telegram suppress:** Khi `OMNI_TELEGRAM_SUPPRESS_WHEN_DEPLOYMENT_HEALTHY=true` và rollout deployment healthy, log không nên có `telegram_escalation_sent` cho cùng điều kiện “đã healthy” — grep analyst/core logs.

## Pass / fail

- **Pass:** Có ít nhất một trace end-to-end với transition hợp lý; **trust but verify** theo checklist (≥2 kênh); nếu scope có **C** thì có bằng chứng terminal hoặc `C skipped` có lý do.
- **Fail:** Ghi `trace_id`, transition lệch, hoặc blocker cluster — không merge tới khi có post-mortem ngắn.
