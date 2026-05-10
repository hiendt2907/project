# E2E: assert advisory qua Telegram Bot API (`getUpdates`)

Nghiệm thu P0: sau khi inject alert (vd. `NS=<ns> bash scripts/gateway_alert_loki_verify.sh`; lab `NS=multi-agent`), **đọc trực tiếp** [getUpdates](https://core.telegram.org/bots/api#getupdates) và assert nội dung tin (trace, VERDICT, độ dài tối thiểu). Log Loki / worker chỉ là **P1** để debug khi API lệch thời gian.

## Telegram Bot API — hạn chế `getUpdates`

Với **supergroup / nhiều chat**, Telegram **không** đưa tin do **chính bot gửi** vào hàng đợi `getUpdates` (đã kiểm chứng: `sendMessage` → `getUpdates` rỗng). Harness vẫn đạt P0 bằng **Bot API**:

1. Thử `getUpdates` (ngắn).
2. Mặc định bật **`deleteMessage`** sau khi đọc `message_id` từ log có `event=telegram_outbound_ok` trên **omni-analyst** — lệnh `deleteMessage` thành công chứng minh tin đã tồn tại trên Telegram (**lab**: tin advisory sẽ bị xóa khỏi chat).

Tắt hành vi xóa: `E2E_TELEGRAM_VERIFY_DELETE_MESSAGE=0`.

## Chuẩn bị

| Biến | Ý nghĩa |
|------|--------|
| `TELEGRAM_BOT_TOKEN` | Bot token (Secret / env), **không** commit |
| `OMNI_TELEGRAM_ENABLED` | `true` trên **omni-analyst** để gửi advisory |
| `OMNI_TELEGRAM_ADMIN_CHAT_ID` | Chat nhận advisory (filter assert mặc định) |
| `OMNI_TELEGRAM_POLLING_ENABLED` | `false` trên cluster (ConfigMap) — **tắt `telegram_loop` trên prober/full** để không tranh `getUpdates` với harness |

Tin advisory có footer `*TRACE:* \`<trace_id>\`` (worker) để assert ổn định.

## Chat / privacy

`getUpdates` chỉ thấy update stream mà Telegram gửi cho bot. Với **supergroup**: thường cần tắt Group Privacy cho bot hoặc dùng **channel** (bot admin) để `channel_post` xuất hiện trong updates. Nếu assert timeout, kiểm tra loại chat và [Bot FAQ](https://core.telegram.org/bots/faq).

## Chạy

```bash
# Sau deploy; prober không long-poll Telegram nếu OMNI_TELEGRAM_POLLING_ENABLED=false
export TELEGRAM_BOT_TOKEN=...
export OMNI_TELEGRAM_ADMIN_CHAT_ID=...
export E2E_ASSERT_TELEGRAM_BOT_API=1
export E2E_TELEGRAM_POLL_SEC=300   # optional; LLM chậm
export NS=multi-agent   # hoặc namespace đích; script exit 2 nếu thiếu NS
bash scripts/gateway_alert_loki_verify.sh
```

Hoặc chỉ assert khi đã có `trace_id`:

```bash
python3 scripts/e2e_telegram_bot_api_assert.py '<trace_id>'
```

## Ánh xạ kịch bản (matrix tối thiểu)

| Kịch bản | Script / entry | Bằng chứng tối thiểu (trust but verify) | Ghi chú |
|----------|-----------------|----------------------------------------|--------|
| **A** Contrast / suggest | `NS=<ns> bash scripts/gateway_alert_loki_verify.sh` + tuỳ `E2E_ASSERT_TELEGRAM_BOT_API=1` | Log pod **+** Loki cùng `trace_id`; Telegram nếu contrast gửi tin | Smoke MPV3 nhanh |
| **B** Full advisory LLM | `NS=<ns> bash scripts/e2e_one_alert_full_advisory_path.sh` | Log `advisory_analyst_ok` / CRAT **+** Bot API hoặc `deleteMessage` OK **+** Loki | Mặc định `nginx_waiting_fault`; sleep dài |
| **C** Death loop / feedback | Sau trace: `scripts/e2e_collect_trace_evidence.sh '<trace_id>'` + inject/stress lab | Counter `action_feedback_*` / `omni_actions_in` **+** dòng terminal (`STATE_VERIFY_MAX_ATTEMPTS` / success / tombstone) **+** log | Không dùng một dòng grep làm PASS duy nhất |
| Proactive + build/deploy | `make e2e-proactive` (`NS=multi-agent` trong Makefile) / `NS=<ns> bash scripts/proactive_e2e.sh` | Theo checklist khi cần Telegram | Có thể nối assert tương tự nếu advisory ra Telegram |
| Ma trận fault | `make e2e-incident-matrix` (`NS=multi-agent` trong Makefile) / `NS=<ns> bash scripts/e2e_incident_matrix.sh` | Tee + Loki + trace | Tăng `SLEEP_SEC` / `E2E_EXTRA_AGENTIC_SLEEP`; bật assert khi cần advisory |
| FinGuard Redis → bridge → Omni | `scripts/verify_e2e_crat_pipeline.py` | CRAT verifier **+** trace log | Cross-stack; assert Telegram **tách** bước nếu ingest không qua gateway — dùng cùng script assert với `trace_id` từ log |

Checklist đầy đủ: [e2e_full_flow_evidence_checklist.md](e2e_full_flow_evidence_checklist.md).

Ràng buộc vận hành: `OMNI_SIEM_SUGGEST_ONLY` / advisory path; CRAT fail-closed trước emit (xem `CLAUDE.md`).

## FinGuard + Omni (cross-stack)

Khi cần E2E từ `stream:actionable_incidents` → Kafka → analyst → Telegram: chạy pipeline FinGuard/SIEM bridge rồi lấy `trace_id` từ log gateway hoặc analyst và gọi `scripts/e2e_telegram_bot_api_assert.py`. Giữ **một egress** theo `smart-siem/docs/CUSTOMER_BANK_SINGLE_EGRESS.md`.

## Song song V15 (không chặn DoD Telegram)

Wave DRM / Buildah / OpenAPI-BFF (`smart-siem/docs/MASTER_PLAN_V15.md`) triển khai song song; **DoD Telegram E2E** là cổng riêng cho luồng advisory hiện tại.
