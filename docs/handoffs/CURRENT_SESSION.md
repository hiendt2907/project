# Current Session Handoff

## Deliverable hiện tại

Vòng 2: đóng gap **vận hành/governance** giữa vision Autonomous SRE và production thật
(tiếp nối vòng 1 kiến trúc AOIP, đã commit `63b20c5`). Plan 5 phase tại
`plans/omni-close-production-gaps-2026-07-23.md`. Phase 1 (đường escalate-cho-người) và
Phase 2 (3 bug omni-core) đã DONE, verify runtime thật trên lab. Phase 3 (SIEM→tier gate),
Phase 4 (tenant-portal parity) CHƯA bắt đầu. Phase 5 (siết RBAC/mutate) CHỈ viết plan,
KHÔNG chạy trong lab theo chỉ thị user.

## Definition of Done

- Phase 1: đường escalate-cho-người (Telegram + Kafka durable + DB persist) chạy thật trên
  lab, có bằng chứng runtime — ĐẠT.
- Phase 2: 3/3 bug omni-core (Prometheus format, deep_scout fail-silent, proactive dormant)
  fix + verify thật trên lab — ĐẠT.
- Phase 3/4: chưa bắt đầu — còn lại cho session sau.
- Phase 5: chỉ cần nội dung plan đủ chi tiết, không thực thi — plan đã viết sẵn, đạt.

## Trạng thái hiện tại

Phase 1 + Phase 2 code xong, đã deploy thật lên `omni-fullstack` (rebuild image + rollout
restart), đã chạy round-trip verify thật trên lab (Telegram/Kafka/Postgres/CRAT thật).
**ĐÃ commit** `07f483b` (2026-07-27), working tree clean. Cluster OrbStack hiện đang
**Stopped** (kubectl connection refused) — cần bật lại trước khi làm Phase 3.

## Đã hoàn thành

- **Phase 1** (`src/workers/advisory_ack.py` mới): user chọn hybrid — giữ Telegram làm kênh
  chính (không hồi sinh Kafka-HITL `omni-hitl-pending`, tránh đảo ngược quyết định kiến
  trúc `advisory_hitl_compat.py`), NHƯNG suggestion durable trên Kafka
  (`omni-advisory-suggestions`, topic đã tạo thật trên lab) + operator ack ghi CRAT
  (`ADVISORY_DECISION`, ký Ed25519) → Kafka → Postgres
  (`omni_admin.advisory_acknowledgment`, migration `0011` đã apply thật).
  Wire vào `telegram_advisory_emitter.py` (nút "✅ Đã ghi nhận") + `omni_worker.py`
  telegram_loop (`advack:` namespace callback riêng).
  **Verify thật**: deploy lại `omni-fullstack`, chạy script one-off trong pod — Telegram
  group thật "trading_system" nhận tin nhắn + nút, Kafka có 2 message thật
  (pending_ack→acknowledged), CRAT có block ký thật, Postgres có row thật. Giới hạn trung
  thực: bước "bấm nút" là mô phỏng (callback_query_id synthetic, Telegram từ chối 400 —
  đúng vì không phải người bấm thật), nhưng CRAT/Kafka/DB đã ghi THÀNH CÔNG trước bước đó.
- **Phase 2** (3/3 bug omni-core, tất cả đã verify runtime thật, không chỉ giả định):
  1. Prometheus `now-1h` format — xác nhận bug thật qua port-forward + curl lab
     (`400 bad_data`). Fix tập trung DUY NHẤT tại `_prometheus_get_json`
     (`sdk_service_tools.py`) — đóng ~10 call-site cùng lúc (forecast, sigma_calibrator,
     query_prometheus_metrics...). Verify lại đúng code path → `status=success`.
  2. `deep_scout.py`: `_retry_redis_write()` (3 lần, backoff) + escalate qua
     `ErrorLedger.record_exception` khi hết retry — không còn nuốt lỗi Redis timeout im lặng.
  3. `proactive_observer.py`: `proactive_promql_rules` (JSON optional trong settings) cho
     phép nhiều rule; rỗng/lỗi fallback fail-closed về đúng 1 rule cũ (không breaking).
- Full test suite sau cả 2 phase: **6673 passed, 0 fail** (baseline vòng 1 là 6640,
  +33 test mới: `test_advisory_ack.py` 12, `test_prometheus_relative_time.py` 9,
  `test_deep_scout_redis_retry_escalate.py` 5, `test_proactive_multi_rule.py` 7).

## Branch và commit

`main`. HEAD `07f483b` (vòng 2 Phase 1+2). Vòng 1 là `63b20c5`.

## Working tree

Clean (`git status --short` rỗng). Toàn bộ Phase 1+2 nằm trong commit `07f483b`
(16 file, +1281/-216).

## Files chính đã thay đổi

- `src/workers/advisory_ack.py` (mới) — module chính Phase 1.
- `src/workers/telegram_advisory_emitter.py`, `src/workers/omni_worker.py` — wire-in Phase 1.
- `migrations/omni_admin/0011_advisory_acknowledgment.sql`,
  `src/services/admin_config/repo.py` — persist ledger Phase 1.
- `scripts/kafka_ensure_omni_topics.sh` — thêm topic `omni-advisory-suggestions`.
- `src/workers/sdk_service_tools.py` (`_resolve_prometheus_time`, `_prometheus_get_json`),
  `src/init/deep_scout.py` (`_retry_redis_write`),
  `src/workers/proactive_observer.py` (`_load_proactive_rules`) — Phase 2, 3 bug fix.
- `plans/omni-close-production-gaps-2026-07-23.md` — plan đầy đủ 5 phase, đã qua
  adversarial review (Opus, sửa 1 CRITICAL + 3 HIGH).

## Quyết định đã chốt

- Phase 1: KHÔNG hồi sinh Kafka-HITL (`omni-hitl-pending`/`hitl_dispatcher`) — Telegram vẫn
  là kênh chính. Hybrid: durable Kafka (`omni-advisory-suggestions`) + DB persist khi ack.
- Phase 3 (kế tiếp) PHẢI route SIEM L3_HITL qua `advisory_ack` vừa xây (Telegram), KHÔNG
  phải `omni-hitl-pending` — theo đúng quyết định Phase 1.
- RBAC/mutate gate: nới lỏng cho Phase 1-4 trong lab (đã dùng để deploy/test thật lần này),
  KHÔNG hỏi lại xin phép mỗi lần trong vòng lab này.
- Phase 5 (siết RBAC/mutate) TUYỆT ĐỐI không tự chạy — chỉ viết plan, chờ user xác nhận
  cutover production thật.
- Không mở lại `FRAMEWORK_LAWS.md` (constitutional-frozen).
- Không tự ý commit/push — hỏi trước mỗi lần (vòng 2 Phase 1+2 đã được duyệt và commit `07f483b`).

## Verification đã chạy

```
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
→ 6673 passed, 11 deselected, 0 fail (chạy lần cuối sau Phase 2, trước checkpoint này)
```

Runtime thật trên lab (không phải chỉ pytest): xem "Đã hoàn thành" — port-forward curl
Prometheus, kubectl exec script one-off trong pod `omni-fullstack`, kafka-console-consumer
đọc trực tiếp broker, `psql` trực tiếp `omni-postgres-0`, `curl` Bot API thật.

## Deployment hiện tại

`omni-fullstack` đã redeploy thật với code Phase 1+2 (rebuild `multi-agent-system:latest` +
`kubectl rollout restart`); code trên pod nay khớp git HEAD `07f483b` — không còn rủi ro
mất đồng bộ git↔cluster. Migration `0011` đã apply thật trên `omni-postgres-0`. Topic
`omni-advisory-suggestions` đã tạo thật trên Kafka lab. **Cluster OrbStack hiện đang
Stopped** — trạng thái pod chưa xác minh lại được cho tới khi bật lại.

## Blockers

Không có blocker kỹ thuật. Cluster OrbStack đang Stopped — phải khởi động lại
(`kubectl get pods -n multi-agent` phải trả về) trước khi verify runtime Phase 3.

## Next step chính xác

Bật lại cluster OrbStack, rồi đọc `plans/omni-close-production-gaps-2026-07-23.md` phần
Phase 3 và bắt đầu: route SIEM L3_HITL qua `advisory_ack.py` (Telegram), đọc lại
`src/services/analyst/chain_consumer.py:318-335` + `src/workers/tier_gate.py` trước khi code.

## Lệnh cần chạy lại

```
git status --short                                                 # xem thay đổi thật
cat plans/omni-close-production-gaps-2026-07-23.md                 # đọc lại plan Phase 3-5
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration     # xác nhận vẫn 6673 pass
kubectl get pod -n multi-agent -l app=omni-fullstack                # xác nhận pod vẫn chạy code mới
```

## Không được làm lại

- Đừng audit lại 18-domain hay `docs/` từ đầu.
- Đừng mở lại `FRAMEWORK_LAWS.md`/Constitution.
- Đừng hồi sinh `omni-hitl-pending`/Kafka-HITL cho Phase 3 — dùng `advisory_ack.py` đã xây.
- Đừng chạy Phase 5 trong lab này dưới bất kỳ lý do gì.
- Đừng deploy lại `omni-fullstack` từ git HEAD cũ (`63b20c5`) mà không commit Phase 1+2
  trước — sẽ xoá mất code đang chạy thật trên pod.
- Đừng tự ý commit/push — hỏi trước mỗi lần.

## Tài liệu liên quan

- `plans/omni-close-production-gaps-2026-07-23.md` — plan đầy đủ vòng 2, nguồn sự thật.
- `docs/architecture/ASSESSMENT_autonomous_sre_v2.md` — nguồn gap gốc (18-domain audit).
- `docs/architecture/FRAMEWORK_LAWS.md` — Constitution, không đổi trong phiên này.
- Memory: `project_production_gaps_plan_2026_07_23.md` (checkpoint 2026-07-23 đã cập nhật).
