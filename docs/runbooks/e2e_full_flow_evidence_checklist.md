# E2E full luồng — trust but verify, death loop, bằng chứng

Runbook **DoD** (definition of done) cho nghiệm thu Omni trên cluster: không chỉ “chạy script xanh”, mà phải **tam giác bằng chứng** và (khi scope yêu cầu) chứng minh **vòng feedback thoát có kiểm soát**.

Tham chiếu nhanh: [e2e_cluster_after_deploy.md](e2e_cluster_after_deploy.md) · [e2e_telegram_bot_api_assert.md](e2e_telegram_bot_api_assert.md) · `scripts/e2e_one_alert_full_advisory_path.sh` · `scripts/gateway_alert_loki_verify.sh` · `scripts/e2e_collect_trace_evidence.sh`.

## Kịch bản (A / B / C)

| ID | Tên | Mục đích | Entry |
|----|-----|----------|--------|
| **A** | Contrast / suggest nhanh | `STATE_MACHINE_CONTRAST` → `SUGGEST_REMEDIATION` + executor | `bash scripts/gateway_alert_loki_verify.sh` (payload HighCPU mặc định) |
| **B** | Advisory LLM đầy đủ | RAG/LLM → CRAT → Telegram advisory + suggest | `bash scripts/e2e_one_alert_full_advisory_path.sh` |
| **C** | Death loop / feedback | `omni-actions` → `omni-action-feedback` → analyst **lặp** tới **terminal** (cap / success / tombstone / REQUIRES_HUMAN) — chứng minh **không treo** | Lab fault có feedback (inject / stress) **hoặc** giảm tạm `OMNI_STATE_VERIFY_MAX_ATTEMPTS` **chỉ lab**; sau đó `scripts/e2e_collect_trace_evidence.sh <trace_id>` |

Ghi trong artifact / PR: đang claim **A**, **B**, hay **C** (hoặc tổ hợp). Chỉ A/B **không** đủ nếu ticket yêu cầu explicit **death-loop**.

## Trust but verify (tối thiểu 2 kênh độc lập)

Đánh dấu [ ] từng hàng; **PASS nghiệm thu** cần ít nhất **hai** dòng tick cho cùng một `trace_id` (trừ khi ghi `SKIP: infra` và downgrade mức).

- [ ] **Gateway / trace:** dòng `trace_id=` + response gateway (tee log).
- [ ] **Pod logs:** `kubectl logs deploy/omni-analyst …` (và prober/executor khi liên quan) có chuỗi trace.
- [ ] **Loki:** query_range hoặc Explore với `|= "<trace_id>"` — snippet JSON hoặc 10–20 dòng.
- [ ] **Telegram (khi bật advisory):** `E2E_ASSERT_TELEGRAM_BOT_API=1` hoặc `python3 scripts/e2e_telegram_bot_api_assert.py '<trace_id>'` — exit 0.
- [ ] **CRAT (khi có key lab):** log `audit_block_written` + `signed=True` **hoặc** `python3 scripts/verify_e2e_crat_pipeline.py` (stdout terminal proof).
- [ ] **Build identity:** `git rev-parse HEAD` + image digest / tag deployment đang chạy.

## Death loop (C) — bằng chứng bắt buộc khi scope = C

Sau khi có `trace_id`:

```bash
export NS=multi-agent
bash scripts/e2e_collect_trace_evidence.sh '<trace_id>' | tee reports/e2e-artifacts/trace-<trace_id>-evidence.txt
```

Kiểm tra trong output:

- `count_action_feedback_received` ≥ 1 khi có mutate/suggest loop thật.
- `count_action_feedback_published` / `count_omni_actions_in` theo kịch bản.
- Dòng **terminal**: một trong `STATE_VERIFY_MAX_ATTEMPTS`, `ESC_MAX_ATTEMPTS_EXCEEDED`, `tombstone`, `action_feedback_success`, `VERIFIED`, `REQUIRES_HUMAN` (tuỳ policy).

Nếu mọi counter feedback = 0 và chỉ một shot suggest → ghi **`C skipped: single-shot path`**; nghiệm thu **C** coi là chưa đạt (trừ PR chỉ A/B).

## Rollback lab (sau C / stress)

- Restore workload / secret / ConfigMap đã đụng.
- Revert tạm `OMNI_STATE_VERIFY_MAX_ATTEMPTS` (chỉ lab).
- `E2E_TELEGRAM_VERIFY_DELETE_MESSAGE=0` nếu cần giữ tin trên chat.

## Lưu artifact

Thư mục gợi ý: `docs/reports/e2e-artifacts/` — chỉ commit **README** + file đã gỡ secret; log raw để ngoài git hoặc ticket. Xem [e2e-artifacts/README.md](../reports/e2e-artifacts/README.md).
